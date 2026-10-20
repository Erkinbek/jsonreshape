"""Export dialog (Ctrl+E): docs/ARCHITECTURE.md §8, §11.8; AGENT_PROMPT §10.

:class:`ExportDialog` writes the current view, all records, the selected records, the current
node or a given record set of a session to JSON, JSON Lines, CSV, Excel (XLSX) or SQLite:

* the output file (save dialog with filters; the format follows the extension,
  :func:`jsonreshape.writer.format_for_path`, or the format combo, which then adjusts the
  extension); the source file and the document's data file are refused;
* the scope: the current view (filter/sort: ``session.view_recordset()``), all records, the
  **selected records** of the active view (the ``selection`` callback -- the bulk dialog's source:
  a ``SelectionScope`` of table rows / tree nodes, asked when the dialog is activated and again when
  the export starts; its rows become positions inside the task, tagged with the pinned state), the
  current **node** -- JSON / JSON Lines of any node through :func:`jsonreshape.writer.export_node`
  (:func:`export_node` here is the thin GUI wrapper: tokens byte-identical, one compact line per
  array element) and CSV / XLSX / SQLite of an array / object node: the file read again with the
  node as the record container (:func:`jsonreshape.gui.filetoolsdialog.node_document`; refused for
  edited documents, JSON Lines / concatenated / brace-list files -- those formats are disabled with
  the reason -- and scalar nodes) -- or the **record set** given with ``records=`` (the duplicates
  dialog's "Eksport…": the user's view is not changed; the dialog owns the set and closes it once
  it closed and its exports ended; a set of an older edit state is refused);
* "keep the wrapper" (wrapped documents) for every format: the wrapper written (JSON), its
  ``_wrapper`` table (SQLite) or, unchecked, no notice that it was not written (JSON Lines / CSV /
  XLSX); not for a node (the node is the whole output) nor with "rebuild keyed";
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
"Current view" cannot be exported while its filter / sort is still running. An output that
another tab shows (review S3, :func:`jsonreshape.gui.editing.target_conflict`) is refused when
that tab has unsaved or pending changes or a running change (or another window of the program
edits the file); when the tab is unchanged the dialog asks first (instead of the overwrite
question) and the tab opens the new file once the export finished.

Editing (docs/ARCHITECTURE.md §12.9): an export writes the **effective** document -- the
file plus the unsaved changes -- as it was when the export was started: the dialog takes
``session.snapshot()`` (a document pinned to the current edit state + the view set whose
positions belong to it) and the session keeps that state readable until the task ended
(``keep_document_for(task, sets, doc=pinned)``), so edits made meanwhile do not leak into
it. The dialog says "saqlanmagan o'zgarishlar ham kiradi" (and so does the report) when
the document is edited. Node exports read composite nodes of an edited document (the root
of an array, a wrapped document's wrapper) through ``doc.node_chunks`` and an array of
records record by record (``doc.iter_record_bytes``), never one byte range of the file
(:func:`jsonreshape.writer.export_node`).

Mapping mode (docs/ARCHITECTURE.md §13.5, the structure builder's "Eksport…" / Ctrl+E):
``ExportDialog(session, parent, mapping=MappingSource(...))`` restructures the records with the
builder's mapping tree -- ``engine.apply`` (JSON or JSON Lines, the indent, "skip very large
records") through the same :class:`ExportRunner` (a heavy task, the document kept for it, the
report window). The check box "Tuzilma bo'yicha qayta qurish" switches the mapping off again
(the ordinary export). With the mapping on, CSV / XLSX / SQLite, the node / selected / record-set
scopes, key sorting, "keep the wrapper" and "rebuild keyed" are not offered (disabled with the reason); the scope
"current view" exports the records of the filtered / sorted view in its order; the default file
name is ``<name>_yangi.json`` (the prototype's); the tree is copied when the export starts, so
later changes in the builder do not reach a running export; the options of the ordinary export
remembered in the GUI state are left alone (only the folder is remembered).
"""

from __future__ import annotations

import dataclasses
import logging
import os
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEvent, QObject, Qt, Signal
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

from jsonreshape import writer
from jsonreshape.errors import Cancelled, EditError, JsonReshapeError, UserInputError
from jsonreshape.fileio import long_path, normalize_path
from jsonreshape.gui import copyfmt, theme
from jsonreshape.gui.searchpanel import check_not_source, keep_for, session_snapshot
from jsonreshape.gui.statusbar import StatusLabel
from jsonreshape.gui.widgets import confirm, format_count, format_size, keep_alive, open_local_path
from jsonreshape.i18n import t
from jsonreshape.recordset import RecordSet, RecordSetBuilder
from jsonreshape.settings import XLSX_MAX_ROWS
from jsonreshape.tasks import CancelToken, Progress, check, format_duration
from jsonreshape.writer import ExportOptions, ExportReport

log = logging.getLogger(__name__)

__all__ = ["ExportDialog", "ExportJob", "ExportOutcome", "ExportReportDialog", "ExportRunner", "FORMATS",
           "MappingSource", "export_node", "export_runner", "mapping_report"]

FORMATS = ("json", "jsonl", "csv", "xlsx", "sqlite", "parquet")
NODE_FORMATS = ("json", "jsonl")                 # any node: writer.export_node
TABLE_NODE_FORMATS = ("csv", "xlsx", "sqlite", "parquet")   # an array / object node re-opened as a record container
MAPPING_FORMATS = ("json", "jsonl")
MAPPING_SUFFIX = "_yangi"          # the prototype's default output name: <name>_yangi.json
SCOPES = ("view", "all", "selected", "node", "records")
EXTENSIONS = {"json": ".json", "jsonl": ".jsonl", "csv": ".csv", "xlsx": ".xlsx", "sqlite": ".sqlite",
              "parquet": ".parquet"}
STATE_KEY = "export_last"
DIR_KEY = "last_export_dir"

_INDENTS: tuple[tuple[str, Any], ...] = (("2", 2), ("4", 4), ("tab", "\t"), ("compact", None))
_SORTS: tuple[tuple[str, Any], ...] = (("none", False), ("alpha", "alpha"), ("codepoint", "codepoint"))
_DELIMITERS: tuple[tuple[str, str], ...] = ((",", ","), (";", ";"), ("Tab", "\t"), ("|", "|"))
_KEYED_LINE = '{"id1": {…}}'
_KEYED_OBJECT = '{"id1": {…}, "id2": {…}}'


# ==================================================================================
# node export (worker thread)
# ==================================================================================
def export_node(session: Any, ref: Any, out_path: str | os.PathLike[str], fmt: str = "json", *,
                indent: int | str | None = 2, progress: Progress | None = None,
                cancel: CancelToken | None = None, doc: Any = None) -> ExportReport:
    """Write one node of ``session`` as JSON / JSON Lines (worker threads only):
    :func:`jsonreshape.writer.export_node` of the node ``ref`` resolved in ``doc`` (default: the
    session's document pinned now; the dialog passes the pinned one it started with, so a reopen or
    an edit meanwhile does not matter). The output may not be the source file, the data file or the
    session's file -- also under another name (``check_not_source``: junctions, shares, hard links).
    Other formats: ``gui.export.node_formats`` (the dialog writes CSV / XLSX / SQLite of an array /
    object node through :func:`jsonreshape.gui.filetoolsdialog.node_document`)."""
    if fmt not in NODE_FORMATS:
        raise UserInputError("gui.export.node_formats")
    if doc is None:
        doc = session_snapshot(session)[0] if session.doc is not None else None
    if doc is None:
        raise UserInputError("gui.session.not_open", name=getattr(session, "name", "?"))
    out = Path(os.path.abspath(os.fspath(out_path)))
    check_not_source(out, (doc.source_path, doc.data_path, getattr(session, "path", None)))
    node = session.node(ref, cancel, doc=doc)
    return writer.export_node(doc, node, out, fmt, indent=indent, progress=progress, cancel=cancel)


def _node_table_export(session: Any, ref: Any, doc: Any, out: str, opts: ExportOptions, *, settings: Any,
                       where: str, progress: Progress | None, cancel: CancelToken | None) -> ExportReport:
    """CSV / XLSX / SQLite of the array / object node ``ref`` (worker threads only): the file opened again
    with the node as the record container (:func:`jsonreshape.gui.filetoolsdialog.node_document`: refused for
    edited documents, JSON Lines / concatenated / brace-list files and scalar nodes), its records exported
    with :func:`jsonreshape.writer.export` (no wrapper: the node is the whole output)."""
    from jsonreshape.gui.filetoolsdialog import node_document

    if progress is not None:
        progress.set_message(t("gui.export.progress.node_open", name=os.path.basename(out)))
    nd = node_document(session, ref, doc, settings=settings, where=where, progress=progress, cancel=cancel)
    try:
        return writer.export(nd, out, dataclasses.replace(opts, keep_wrapper=False), None, progress=progress,
                             cancel=cancel)
    finally:
        nd.close()


def _selected_set(scope: Any, gen: int | None, label: str, cancel: CancelToken | None) -> RecordSet:
    """The positions of a "selected records" scope (a ``bulkdialog.SelectionScope``, a RecordSet, ``range``,
    ``array('Q')`` / list: anything with ``iter_chunks()`` or iterable) as a RecordSet tagged with ``gen``
    (worker threads only; the rows of a view selection are turned into positions here, not in the GUI)."""
    builder = RecordSetBuilder("selected", label, gen=gen)
    try:
        chunks = scope.iter_chunks() if hasattr(scope, "iter_chunks") else [scope]
        for chunk in chunks:
            check(cancel)
            builder.extend(chunk)
        return builder.finish()
    except BaseException:
        builder.abort()
        raise


class _RecordsLease(QObject):
    """The record set of the "records" scope (e.g. the duplicates), owned by the export dialog: closed once the
    dialog released it (closed, destroyed, its document closed) AND every export reading it ended. A child of the
    application's :class:`ExportRunner` (it outlives the dialog) that follows the runner's ``job_finished``."""

    def __init__(self, records: RecordSet, runner: ExportRunner) -> None:
        super().__init__(runner)
        self.records: RecordSet | None = records
        self.runner = runner
        self.jobs: list[ExportJob] = []
        self.released = False
        runner.job_finished.connect(self._job_finished)

    def add_job(self, job: ExportJob) -> None:
        self.jobs.append(job)

    def release(self, *_args: Any) -> None:
        self.released = True
        self._maybe_close()

    def _job_finished(self, job: Any) -> None:
        if any(j is job for j in self.jobs):
            self.jobs = [j for j in self.jobs if j is not job]
            self._maybe_close()

    def _maybe_close(self) -> None:
        if not self.released or self.records is None:
            return
        if any(getattr(j, "state", "") == "running" for j in self.jobs):
            return
        records, self.records = self.records, None
        records.close()
        try:
            self.runner.job_finished.disconnect(self._job_finished)
        except (RuntimeError, TypeError):  # pragma: no cover - the runner is gone
            pass
        self.deleteLater()


# ==================================================================================
# report
# ==================================================================================
@dataclass
class MappingSource:
    """The mapping an :class:`ExportDialog` applies (the structure builder, docs §13.5).

    ``tree``: returns a private copy of the mapping tree (called in the GUI thread when the
    export starts); ``summary``: the builder's stats line ("N maydon · M o'zgarish");
    ``compact``: the builder's "Ixcham" (the compact indent is preselected)."""

    tree: Callable[[], Any]
    summary: str = ""
    compact: bool = False


def mapping_report(fmt: str, report: Any) -> ExportReport:
    """An ``engine.ApplyReport`` as the writer's :class:`ExportReport` (the report window):
    records = records read, rows = items written."""
    return ExportReport(fmt=fmt, records=int(report.records_in), rows=int(report.items_out),
                        bytes_written=int(report.bytes_written), files=list(report.files),
                        warnings=list(report.warnings), elapsed=float(report.elapsed),
                        skipped=int(report.skipped_huge), warning_keys=list(report.warning_keys))


@dataclass
class ExportOutcome:
    """What an export task returns: the writer's report, the size of every file, the
    scope exported."""

    report: ExportReport
    file_sizes: list[int] = field(default_factory=list)
    scope: str = "view"
    path: str = ""
    edited: bool = False          # the document had unsaved changes (they were exported too)
    apply_report: Any = None      # mapping mode: the engine's ApplyReport


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
        applied = self.outcome.apply_report
        if applied is not None:
            text = t("gui.export.report.mapping", records=format_count(report.records),
                     items=format_count(report.rows), passes=format_count(int(getattr(applied, "passes", 1) or 1)),
                     size=format_size(report.bytes_written), seconds=format_duration(report.elapsed),
                     rate=f"{float(getattr(applied, 'mb_per_s', 0.0) or 0.0):.1f}")
            if self.outcome.edited:
                text += "\n" + t("gui.export.report.edited")
            return text
        text = t("gui.export.report.summary", records=format_count(report.records), rows=format_count(report.rows),
                 size=format_size(report.bytes_written), seconds=format_duration(report.elapsed),
                 rate=f"{report.mb_per_s:.1f}")
        if report.skipped:
            text += "\n" + t("gui.export.report.skipped", count=format_count(report.skipped))
        if report.tables:
            text += "\n" + t("gui.export.report.tables", tables=", ".join(report.tables[:20]))
        if self.outcome.edited:
            text += "\n" + t("gui.export.report.edited")
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
    reload: list = field(default_factory=list)    # weak refs: tabs showing the output file (reloaded when done)


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
              parents: tuple = (), sets: tuple = (), doc: Any = None, reload: Any = ()) -> ExportJob:
        """Run ``work(ctx) -> ExportOutcome`` as an export task; the session keeps its
        document -- the edit state of ``doc`` (the pinned document the task reads) and the
        record sets in ``sets`` (the view the task writes) -- open until the task ended,
        also while it still waits in the heavy-task queue. ``reload``: weak references of
        sessions showing the output file, opened again when the export is done."""
        job = ExportJob(session=session, path=path, scope=scope, parents=list(parents), reload=list(reload or ()))
        job.task = self.app.tasks.run(work, title=title, kind="export", owner=job, heavy=True,
                                      on_done=lambda outcome, j=job: self._done(j, outcome),
                                      on_error=lambda exc, j=job: self._failed(j, exc),
                                      on_cancel=lambda j=job: self._cancelled(j))
        keep_for(session, job.task, sets=sets, doc=doc)
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
        if job.reload:
            from jsonreshape.gui.editing import reload_sessions

            reload, job.reload = job.reload, []
            reload_sessions(reload)       # tabs that showed the replaced output file (review S3)
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
    """Export the view / all records / the selected records / the current node / a given record set of
    ``session`` (module docstring).

    ``columns``: the table's visible columns (CSV/XLSX column list; ``None`` = automatic);
    ``scope``: initial scope ``view`` | ``all`` | ``selected`` | ``node`` | ``records``; ``mapping``:
    export with the structure builder's mapping (:class:`MappingSource`, see the module docstring);
    ``selection``: ``fn() -> scope | None`` giving the records selected in the active view now (the bulk
    dialog's source: a ``SelectionScope``, RecordSet, ``range`` or positions; asked when the dialog is
    activated and when the export starts); ``records`` / ``records_label``: a RecordSet of positions of the
    state shown (e.g. the duplicates) offered as the scope "records" -- the dialog owns it (closed after the
    dialog closed and its exports ended). Signals: ``export_started(task)``, ``export_finished(outcome)``,
    ``export_failed(exc)``.
    """

    export_started = Signal(object)
    export_finished = Signal(object)
    export_failed = Signal(object)

    def __init__(self, session: Any, parent: QWidget | None = None, *, columns: list[str] | None = None,
                 scope: str = "view", mapping: MappingSource | None = None,
                 selection: Callable[[], Any] | None = None, records: RecordSet | None = None,
                 records_label: str = "") -> None:
        super().__init__(parent)
        self.session = session
        self.app = session.app
        self.columns = [str(c) for c in columns] if columns else None
        self.mapping = mapping
        self.selection = selection
        self.runner = export_runner(self.app)
        self.records_label = str(records_label or "")
        self._records: _RecordsLease | None = _RecordsLease(records, self.runner) if records is not None else None
        self.job: ExportJob | None = None
        self.task: Any = None
        self.outcome: ExportOutcome | None = None
        self.error: BaseException | None = None
        self.report_dialog: ExportReportDialog | None = None
        self._syncing = False
        self._valid = False
        self._adopted = False               # the job shown was started by another export dialog
        self._queued_shown = False
        self._path_edited = False           # the user typed / chose the output path
        self._selected_count = 0            # records selected in the active view when last asked
        title = "gui.export.mapping.title" if mapping is not None else "gui.export.title"
        self.setWindowTitle(t(title, name=session.name))
        self.setSizeGripEnabled(True)
        if self._records is not None:
            self.destroyed.connect(self._records.release)     # also when deleted without done()
        self._build()
        self._load_defaults()
        self.set_scope(scope if scope in SCOPES else "view")
        if mapping is not None:
            self._init_mapping(mapping)
        if not self.path_edit.text():
            self.path_edit.setText(self._default_path())
        self._path_edited = False
        self.app.tasks.task_updated.connect(self._on_task_updated)
        self.runner.job_finished.connect(self._job_finished)
        theme.notifier().theme_changed.connect(self._style_labels)
        for signal in (session.view_changed, session.count_changed, session.view_busy_changed,
                       session.current_changed, session.state_changed):
            signal.connect(self._session_changed)
        data_changed = getattr(session, "data_changed", None)
        if data_changed is not None and hasattr(data_changed, "connect"):
            data_changed.connect(self._session_changed)      # edits: counts and the "edited" note
        self._validate()
        self._adopt_running_job()
        self.resize(620, self.sizeHint().height())

    @property
    def records(self) -> RecordSet | None:
        """The record set of the scope "records" (``None``: none given, or released)."""
        lease = self._records
        return lease.records if lease is not None and not lease.released else None

    def _release_records(self) -> None:
        lease = self._records
        if lease is not None and not lease.released:
            lease.release()

    def _session_changed(self, *_args: Any) -> None:
        if getattr(self.session, "closed", False) or getattr(self.session, "state", "") == "closed":
            self._release_records()         # positions of a closed document mean nothing any more
        self._update_scope_texts()
        scope = self.scope()
        if not self.scope_buttons[scope].isEnabled() and scope != "view":
            self.set_scope("view")          # the node / the selection / the record set went away
        self._update_format_items()
        self._update_format_widgets()
        self._validate()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt name
        """Activated again (e.g. after selecting rows in the views): the "selected records" count is asked
        again."""
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self._update_scope_texts()
            self._validate()

    def done(self, result: int) -> None:  # noqa: D401 - Qt name
        """Close: the record set of the scope "records" is released (closed once the exports reading it
        ended); a running export goes on."""
        self._release_records()
        super().done(result)

    # -------------------------------------------------------------------------------- build
    def _build(self) -> None:
        layout = QVBoxLayout(self)

        # mapping mode (the structure builder): on / off, its summary, "skip very large records"
        self.mapping_check = QCheckBox(self)
        self.mapping_check.setObjectName("export_mapping")
        self.mapping_check.setVisible(False)
        self.mapping_check.toggled.connect(self._on_mapping_toggled)
        layout.addWidget(self.mapping_check)

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
            button.setObjectName(f"export_scope_{scope}")
            self.scope_group.addButton(button, i)
            self.scope_buttons[scope] = button
            scope_layout.addWidget(button)
        self.scope_buttons["records"].setVisible(self._records is not None)   # only for a given record set
        self.scope_group.idToggled.connect(self._on_scope_toggled)
        layout.addWidget(scope_box)
        self.edited_label = QLabel(t("gui.export.edited_note"), self)
        self.edited_label.setObjectName("export_edited_note")
        self.edited_label.setWordWrap(True)
        self.edited_label.setTextFormat(Qt.TextFormat.PlainText)
        self.edited_label.setVisible(False)
        layout.addWidget(self.edited_label)

        self.options_box = QGroupBox(t("gui.export.options.title"), self)
        box_layout = QVBoxLayout(self.options_box)
        self.pages = QStackedWidget(self.options_box)
        self.pages.addWidget(self._json_page())
        self.pages.addWidget(self._jsonl_page())
        self.pages.addWidget(self._table_page())
        self.pages.addWidget(self._sqlite_page())
        box_layout.addWidget(self.pages)
        # "keep the wrapper" applies to every format (the writer honours it for JSON Lines / CSV / XLSX /
        # SQLite too: the wrapper table, or the notice that the wrapper was not written)
        self.keep_wrapper_check = QCheckBox(t("gui.export.keep_wrapper"), self.options_box)
        self.keep_wrapper_check.setObjectName("export_keep_wrapper")
        self.keep_wrapper_check.setChecked(True)
        self.keep_wrapper_check.toggled.connect(self._validate)
        box_layout.addWidget(self.keep_wrapper_check)
        layout.addWidget(self.options_box, 1)
        self.skip_huge_check = QCheckBox(t("gui.export.mapping.skip_huge"), self)
        self.skip_huge_check.setObjectName("export_skip_huge")
        self.skip_huge_check.setVisible(False)
        self.skip_huge_check.toggled.connect(self._validate)
        layout.addWidget(self.skip_huge_check)

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
        suffix = MAPPING_SUFFIX if self.mapping_active() else "-export"
        return os.path.join(folder, f"{stem}{suffix}{EXTENSIONS[self.format()]}")

    # -------------------------------------------------------------------------------- mapping mode
    def mapping_active(self) -> bool:
        """The export restructures the records with the builder's mapping."""
        return self.mapping is not None and self.mapping_check.isChecked()

    def _init_mapping(self, mapping: MappingSource) -> None:
        summary = mapping.summary.strip()
        self.mapping_check.setText(t("gui.export.mapping.use", summary=summary) if summary
                                   else t("gui.export.mapping.use_plain"))
        self.mapping_check.setToolTip(t("gui.export.mapping.use.tip"))
        self.mapping_check.setVisible(True)
        self.skip_huge_check.setToolTip(t("gui.export.mapping.skip_huge.tip",
                                          limit=format_size(int(self.app.settings.large_node_bytes))))
        self._syncing = True
        try:
            self.mapping_check.setChecked(True)
            # the prototype's output: <name>_yangi.json (not the format of the last ordinary export)
            self.format_combo.setCurrentIndex(FORMATS.index("json"))
            if mapping.compact:
                self._select_data(self.indent_combo, None)
        finally:
            self._syncing = False
        self._update_mapping_widgets()

    def _on_mapping_toggled(self, _on: bool) -> None:
        if self._syncing:
            return
        self._update_mapping_widgets()
        if not self._path_edited:
            self._syncing = True
            try:
                self.path_edit.setText(self._default_path())
            finally:
                self._syncing = False
        self._validate()

    def _update_mapping_widgets(self) -> None:
        active = self.mapping_active()
        title = "gui.export.mapping.title" if active else "gui.export.title"
        self.setWindowTitle(t(title, name=self.session.name))
        if active and self.format() not in MAPPING_FORMATS:
            self.set_format("json")
        if active and self.scope() not in ("view", "all"):
            self.scope_buttons["view"].setChecked(True)     # the mapping restructures the view / all records
        self.skip_huge_check.setVisible(active)
        self._update_scope_texts()
        self._update_format_items()
        self._update_format_widgets()

    def _format_refusal(self, fmt: str) -> str | None:
        """Why ``fmt`` is not offered for the scope / the mapping (localized), or ``None``: the mapping
        writes JSON / JSON Lines only; the node scope writes CSV / XLSX / SQLite of an array / object node by
        reading the file again with the node as the record container -- not for an edited document or a JSON
        Lines / concatenated / brace-list file (:func:`jsonreshape.gui.filetoolsdialog.check_node_source`)."""
        if self.mapping_active():
            return None if fmt in MAPPING_FORMATS else t("gui.export.mapping.formats")
        if self.scope() == "node" and fmt in TABLE_NODE_FORMATS:
            doc = self.session.doc
            if doc is None:
                return None
            from jsonreshape.gui.filetoolsdialog import check_node_source

            try:
                check_node_source(doc)
            except JsonReshapeError as exc:
                return exc.message
        return None

    def _update_format_items(self) -> None:
        """Formats the scope and the mapping allow (the others disabled with the reason)."""
        for i, fmt in enumerate(FORMATS):
            why = self._format_refusal(fmt)
            _set_item_enabled(self.format_combo, i, why is None, why)

    def apply_options(self) -> Any:
        """The ``engine.ApplyOptions`` of the mapping mode (``UserInputError`` when invalid)."""
        from jsonreshape.engine import ApplyOptions

        fmt = self.format()
        indent = self.indent_combo.currentData() if fmt == "json" else None
        return ApplyOptions(jsonl=fmt == "jsonl", indent=indent, skip_huge=self.skip_huge_check.isChecked())

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
        """Choose ``view`` | ``all`` | ``selected`` | ``node`` | ``records``; a scope that is not available now
        (no current node, nothing selected, no record set, the mapping mode) chooses ``view``."""
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
        if not node:
            # every format: the wrapper written (JSON), its table (SQLite) or the notice that it was not
            # (JSON Lines / CSV / XLSX); a node is the whole output (no wrapper)
            kw["keep_wrapper"] = self.keep_wrapper_check.isChecked()
        if fmt == "json":
            kw["indent"] = self.indent_combo.currentData()
            if not node:
                kw["sort_keys"] = self.json_sort_combo.currentData()
                kw["as_keyed"] = self.as_keyed_check.isChecked()
        elif fmt == "jsonl":
            if not node:
                kw["sort_keys"] = self.jsonl_sort_combo.currentData()
        elif fmt in ("csv", "xlsx", "parquet"):
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
            elif fmt == "xlsx":
                kw["xlsx_split"] = self.split_combo.currentData()
                kw["xlsx_max_rows"] = self.max_rows_spin.value()
        return ExportOptions.from_settings(fmt, self.app.settings, **kw)

    # -------------------------------------------------------------------------------- reactions
    @staticmethod
    def _page_index(fmt: str) -> int:
        return {"json": 0, "jsonl": 1, "csv": 2, "xlsx": 2, "parquet": 2, "sqlite": 3}[fmt]

    def _on_path_changed(self, text: str) -> None:
        if self._syncing:
            self._validate()
            return
        self._path_edited = True
        fmt = writer.format_for_path(text.strip()) if text.strip() else None
        if fmt is not None and fmt != self.format():
            index = FORMATS.index(fmt)
            if self._format_refusal(fmt) is None:
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
        mapped = self.mapping_active()
        keyed_out = fmt == "json" and self.as_keyed_check.isChecked()      # as_keyed drops the wrapper
        self.keep_wrapper_check.setEnabled(wrapped and not node and not mapped and not keyed_out)
        self.keep_wrapper_check.setToolTip(t("gui.export.mapping.not_used") if mapped
                                           else t("gui.export.keep_wrapper.node") if node and wrapped
                                           else t(f"gui.export.keep_wrapper.tip.{fmt}") if wrapped
                                           else t("gui.export.keep_wrapper.only_wrapped"))
        self.as_keyed_check.setEnabled(not node and not mapped)
        self.as_keyed_check.setToolTip(t("gui.export.mapping.not_used") if mapped
                                       else t("gui.export.as_keyed.tip", line=_KEYED_LINE, result=_KEYED_OBJECT))
        for combo in (self.json_sort_combo, self.jsonl_sort_combo):
            combo.setEnabled(not node and not mapped)
            combo.setToolTip(t("gui.export.mapping.not_used") if mapped
                             else t("gui.export.node_no_sort") if node else t("gui.export.sort.tip"))

    def _on_scope_toggled(self, _id: int, checked: bool) -> None:
        if not checked:
            return
        self._update_format_items()
        if self._format_refusal(self.format()) is not None:
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
        # the records selected in the active view (asked now; the export asks again when it starts)
        sel_btn = self.scope_buttons["selected"]
        count = self._selected_count = self._selection_count()
        if count:
            sel_btn.setText(t("gui.export.scope.selected", count=format_count(count)))
            sel_btn.setEnabled(True)
            sel_btn.setToolTip(t("gui.export.scope.selected.tip"))
        else:
            sel_btn.setText(t("gui.export.scope.selected_none"))
            sel_btn.setEnabled(False)
            sel_btn.setToolTip(t("gui.export.scope.selected_none.tip") if not single
                               else t("gui.export.scope.single.tip"))
        rec_btn = self.scope_buttons["records"]
        records = self.records
        if records is not None:
            label = self.records_label or t("gui.export.scope.records_default")
            rec_btn.setText(copyfmt.escape_surrogates(t("gui.export.scope.records", label=label,
                                                        count=format_count(len(records)))).replace("&", "&&"))
            rec_btn.setEnabled(True)
            rec_btn.setToolTip(t("gui.export.scope.records.tip"))
        else:
            rec_btn.setText(t("gui.export.scope.records_gone"))
            rec_btn.setEnabled(False)
            rec_btn.setToolTip(t("gui.export.scope.records_gone"))
        if self.mapping_active():
            node_btn.setEnabled(False)
            node_btn.setToolTip(t("gui.export.mapping.no_node"))
            for key in ("selected", "records"):
                self.scope_buttons[key].setEnabled(False)
                self.scope_buttons[key].setToolTip(t("gui.export.mapping.no_scope"))

    def _selection_scope(self) -> Any:
        """The records selected in the active view now (``None``: nothing selected, no callback, a single
        document -- its one record is "all records")."""
        doc = self.session.doc
        if self.selection is None or doc is None or doc.kind == "single" or getattr(self.session, "closed", False):
            return None
        try:
            scope = self.selection()
        except JsonReshapeError as exc:
            log.info("the selection is not available: %s", exc)
            return None
        except Exception:
            log.exception("the selection callback failed")
            return None
        if scope is None:
            return None
        try:
            return scope if len(scope) else None
        except TypeError:
            return scope

    def _shown_gen(self) -> int | None:
        """The edit state the views show (the edit controller's ``applied_gen``, else ``data_gen``)."""
        edit = getattr(self.session, "edit", None)
        gen = getattr(edit, "applied_gen", None) if edit is not None else None
        if gen is None:
            gen = getattr(self.session, "data_gen", None)
        return None if gen is None else int(gen)

    def _selection_count(self) -> int:
        scope = self._selection_scope()
        if scope is None:
            return 0
        try:
            return int(len(scope))
        except TypeError:
            return 1

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
        mapped = self.mapping_active()
        if scope == "node":
            if mapped:
                errors.append(t("gui.export.mapping.no_node"))
            elif session.current is None:
                errors.append(t("gui.export.scope.node_none.tip"))
            elif self.format() in TABLE_NODE_FORMATS:
                why = self._format_refusal(self.format())
                if why:
                    errors.append(why)
        elif scope == "selected":
            if mapped:
                errors.append(t("gui.export.mapping.no_scope"))
            elif not self._selected_count:
                errors.append(t("gui.export.scope.selected_none.tip"))
        elif scope == "records":
            records = self.records
            if mapped:
                errors.append(t("gui.export.mapping.no_scope"))
            elif records is None or records.closed:
                errors.append(t("gui.export.scope.records_gone"))
            elif doc is not None and records.gen is not None and records.gen != int(getattr(doc, "gen", 0) or 0):
                # the record numbers belong to an older edit state (the duplicates were found before an edit)
                errors.append(EditError("edits.error.stale_scope").message)
        if mapped and self.format() not in MAPPING_FORMATS:
            errors.append(t("gui.export.mapping.formats"))
        if mapped and path and writer.gzip_path(path):
            # engine.apply writes plain JSON: a ".gz" name would hold uncompressed data (the CLI refuses it too)
            errors.append(t("gui.export.mapping.gzip"))
        try:
            if mapped:
                self.apply_options()
            else:
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
        edited = self.edited()
        if self.edited_label.isHidden() == edited:
            self.edited_label.setVisible(edited)
        self.error_label.setText(copyfmt.escape_surrogates("\n".join(errors)))
        self.error_label.setVisible(bool(errors))
        self.warning_label.setText("\n".join(warnings))
        self.warning_label.setVisible(bool(warnings))
        self._valid = not errors
        self.export_button.setEnabled(self._valid and not self._own_running())
        return self._valid

    def error_text(self) -> str:
        return self.error_label.text() if not self.error_label.isHidden() else ""

    def edited(self) -> bool:
        """The document shows unsaved changes: the export includes them (the dialog says so)."""
        doc = self.session.doc if not getattr(self.session, "closed", False) else None
        return doc is not None and bool(getattr(doc, "edited", False))

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
        allowed = [t(f"gui.export.filter.{f}") for f in FORMATS if self._format_refusal(f) is None]
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
        if self.mapping_active():
            return self._start_mapping_export()
        session = self.session
        out = self.output_path()
        fmt = self.format()
        scope = self.scope()
        try:
            opts = self.export_options()
        except JsonReshapeError as exc:  # pragma: no cover - validated above
            self.error_label.setText(exc.message)
            self.error_label.setVisible(True)
            return None
        live = session.doc
        try:
            # also another name of the source (junction, share, hard link): checked once here
            check_not_source(out, (live.source_path, live.data_path, session.path))
        except UserInputError as exc:
            self.error_label.setText(exc.message + " " + t("gui.export.same_file.hint"))
            self.error_label.setVisible(True)
            return None
        reload = self._open_target_check(out)
        if reload is None:
            return None
        if not reload and os.path.exists(long_path(out)) and not confirm(self, t("gui.export.overwrite", path=out)):
            return None
        # the state shown NOW (§12.9): the pinned document and the view set of the same edit
        # state; positions, the selection and the current node are taken together with them
        selected = self._selection_scope() if scope == "selected" else None
        if scope == "selected" and selected is None:
            self.error_label.setText(t("gui.export.scope.selected_none.tip"))
            self.error_label.setVisible(True)
            return None
        records = self.records if scope == "records" else None
        if scope == "records" and (records is None or records.closed):
            self.error_label.setText(t("gui.export.scope.records_gone"))
            self.error_label.setVisible(True)
            return None
        doc, view_set = session_snapshot(session)
        ref = session.current
        edited = bool(getattr(doc, "edited", False))
        doc_gen = getattr(doc, "gen", None)
        if selected is not None:
            sel_gen = getattr(selected, "gen", None)
            if sel_gen is not None and sel_gen not in (doc_gen, self._shown_gen()):
                # positions of another edit state (a selection source that did not follow an edit)
                self.error_label.setText(EditError("edits.error.stale_scope").message)
                self.error_label.setVisible(True)
                return None
        self._remember(opts)
        view = None
        held: list[Any] = []
        if scope == "view" and not session.view_is_base:
            # leased now (session.keep_document_for, through the runner): the session does
            # not close this record set before the task ended -- also when the view is
            # replaced while the task waits in the heavy-task queue -- so the task reads it
            # as it is now, without a copy
            view = view_set
            held.append(view)
        elif selected is not None:
            source = getattr(selected, "source", None)      # the view set the selected rows index
            if isinstance(source, RecordSet):
                held.append(source)
        indent = opts.indent
        settings = self.app.settings
        where = ""
        if scope == "node" and fmt in TABLE_NODE_FORMATS:
            try:
                where = session.location_text(ref)
            except Exception:  # pragma: no cover - a title only
                where = ""
        label = t("gui.export.scope.selected_label")

        def work(ctx: Any) -> ExportOutcome:
            if scope == "node":
                if fmt in NODE_FORMATS:
                    report = export_node(session, ref, out, fmt, indent=indent, progress=ctx.progress,
                                         cancel=ctx.cancel, doc=doc)
                else:
                    report = _node_table_export(session, ref, doc, out, opts, settings=settings, where=where,
                                                progress=ctx.progress, cancel=ctx.cancel)
            elif scope == "selected":
                # rows of the view -> positions (here, not in the GUI thread), tagged with the pinned state:
                # the selection was taken together with it
                rs = _selected_set(selected, doc_gen, label, ctx.cancel)
                try:
                    report = writer.export(doc, out, opts, rs, progress=ctx.progress, cancel=ctx.cancel)
                finally:
                    rs.close()
            elif scope == "records":
                # writer._pin refuses a set of another edit state (edits.error.stale_scope)
                report = writer.export(doc, out, opts, records, progress=ctx.progress, cancel=ctx.cancel)
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
            return ExportOutcome(report=report, file_sizes=sizes, scope=scope, path=out, edited=edited)

        self.outcome = None
        self.error = None
        self.report_dialog = None
        self.job = self.runner.start(session, work, path=out, scope=scope,
                                     title=t("gui.export.task", name=os.path.basename(out)),
                                     parents=(self, self.parentWidget()), sets=tuple(held), doc=doc, reload=reload)
        if records is not None and self._records is not None:
            self._records.add_job(self.job)      # the set stays open until this export ended
        self.task = self.job.task
        self._adopted = False
        self._show_running()
        self.export_started.emit(self.task)
        return self.task

    def _open_target_check(self, out: str) -> list | None:
        """The output file is shown by another tab (review S3): ``None`` -- refused (that tab has unsaved / pending
        changes or a running change, or another window edits the file; the error line says why) or not confirmed;
        else the weak references of the tabs to reload afterwards (``[]``: nobody shows it). The question replaces
        the overwrite question."""
        from jsonreshape.gui.editing import target_conflict

        found = target_conflict(out, exclude=self.session)
        if found is None:
            return []
        what, sessions = found
        path = os.path.normpath(out)
        if what != "open":
            # unsaved / locked / pending (changes kept for recovery): the texts of every tool output
            from jsonreshape.gui.searchpanel import target_conflict_text

            text = target_conflict_text(what, out)
            self.error_label.setText(copyfmt.escape_surrogates(text))
            self.error_label.setVisible(True)
            return None
        if not confirm(self, t("gui.edit.target.open", path=path)):
            return None
        return [weakref.ref(s) for s in sessions]

    def _start_mapping_export(self) -> Any:
        """Mapping mode: ``engine.apply`` of a copy of the builder's tree (taken now) over the
        pinned document (and the view's records in their order for the scope "current view")."""
        from jsonreshape import engine

        session = self.session
        out = self.output_path()
        fmt = self.format()
        scope = "all" if self.scope() == "all" else "view"
        try:
            opts = self.apply_options()
            tree = self.mapping.tree() if self.mapping is not None else None
        except JsonReshapeError as exc:
            self.error_label.setText(exc.message)
            self.error_label.setVisible(True)
            return None
        if tree is None:
            self.error_label.setText(t("gui.export.mapping.no_tree"))
            self.error_label.setVisible(True)
            return None
        live = session.doc
        try:
            check_not_source(out, (live.source_path, live.data_path, session.path))
        except UserInputError as exc:
            self.error_label.setText(exc.message + " " + t("gui.export.same_file.hint"))
            self.error_label.setVisible(True)
            return None
        reload = self._open_target_check(out)
        if reload is None:
            return None
        if not reload and os.path.exists(long_path(out)) and not confirm(self, t("gui.export.overwrite", path=out)):
            return None
        self._remember_dir()
        doc, view_set = session_snapshot(session)
        edited = bool(getattr(doc, "edited", False))
        view = view_set if scope == "view" and not session.view_is_base else None

        def work(ctx: Any) -> ExportOutcome:
            if view is not None and view.closed:   # pragma: no cover - the lease prevents it
                raise UserInputError("gui.search.view_changed")
            report = engine.apply(doc, tree, out, opts, view, progress=ctx.progress, cancel=ctx.cancel)
            sizes = []
            for p in report.files:
                try:
                    sizes.append(os.stat(long_path(p)).st_size)
                except OSError:  # pragma: no cover - just written
                    sizes.append(0)
            return ExportOutcome(report=mapping_report(fmt, report), file_sizes=sizes, scope=scope, path=out,
                                 edited=edited, apply_report=report)

        self.outcome = None
        self.error = None
        self.report_dialog = None
        self.job = self.runner.start(session, work, path=out, scope=scope,
                                     title=t("gui.export.mapping.task", name=os.path.basename(out)),
                                     parents=(self, self.parentWidget()), sets=(view,), doc=doc, reload=reload)
        self.task = self.job.task
        self._adopted = False
        self._show_running()
        self.export_started.emit(self.task)
        return self.task

    def _remember_dir(self) -> None:
        """Mapping mode remembers only the folder (the ordinary export's options stay)."""
        state = self.app.state
        try:
            state.set(DIR_KEY, os.path.dirname(self.output_path()))
        except (TypeError, ValueError):  # pragma: no cover - plain JSON values
            return
        save_state = getattr(self.app, "save_state", None)
        if callable(save_state):
            save_state()

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
