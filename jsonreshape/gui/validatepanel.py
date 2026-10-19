"""JSON Schema in the GUI: generate a schema from a document, validate a document against one
(docs/ARCHITECTURE.md §14.3, §14.9; design: the stage-5 analysis ``s5/map_stats.md`` §8.7; core:
:mod:`jsonreshape.jschema`).

:class:`GenerateSchemaDialog` -- Tools ▸ "Schema yaratish…"
===========================================================
A small non-modal dialog for one :class:`~jsonreshape.gui.session.DocSession`:

* the data the schema is made from: the **cached full analysis** of the shared schema cache
  (:func:`jsonreshape.gui.fields.cached_schema` with ``full=True``; offered only while it is fresh,
  :func:`~jsonreshape.gui.fields.is_stale`), a **full scan** (every record: ``infer_schema`` inside the
  task) or a **sample** of ``Settings.sample_size`` records (the cached schema when fresh, else computed
  in the task) -- allowed, with a visible warning (rare fields may be missing or wrongly "required");
* what it describes: the whole document (``document=True``, the default) or one record; "strict"
  constraints (min/max, enum, formats, no extra members); the title (default: the file name; empty = no
  title);
* "Saqlash…" asks for the file (the injected ``ask_save_path``; default ``<stem>.schema.json`` beside the
  document), refuses the document's own file / data file (also under another name:
  :func:`~jsonreshape.gui.searchpanel.check_not_source`) and a file that another tab shows with unsaved,
  pending or running changes or that another window of the program edits (review S3,
  :func:`~jsonreshape.gui.searchpanel.open_target_check`; a tab showing it unchanged is asked about and
  opens the new file once it was written), then runs ONE task on the document pinned at that moment
  (``session.snapshot()``, kept readable with ``keep_document_for``): the scan when needed,
  :func:`jschema.generate_schema` and :func:`jschema.dump_schema` (exact numbers), written atomically
  (``fileio.atomic_write``). A scan made by the task is handed to the GUI thread (``TaskContext.post``) and
  stored in the shared schema cache (:func:`jsonreshape.gui.fields.store`: as the full analysis when it read
  every record, else as the sample; nothing is stored when the document changed meanwhile), so the schema
  view and the next generation reuse it. The task is heavy when it reads the data (scan or strict). It
  belongs to a small job object, not to the dialog: closing the dialog does not stop it, the result is told
  in the status bar (and in the dialog while it is open); "To'xtatish" and the tasks panel cancel it
  (nothing is written).

:class:`ValidatePanel` -- Tools ▸ "Schema bo'yicha tekshirish…"
================================================================
A widget for a dock or a window (:class:`ValidateDialog` wraps it for one session). It keeps the results
per session (:meth:`ValidatePanel.set_session` switches to that tab's results; the inputs -- schema file
and options -- belong to the panel and are remembered in the GUI state under ``validate_last``):

* the schema file (a line edit + "Tanlash…" with the injected ``ask_open_path``); every change is checked
  in a silent task (``load_schema`` + ``compile_schema``: a valid schema shows its title and size, an
  invalid or unsupported one the localized error inline);
* options: how the schema applies (``scope`` auto / record / document), which records (all, or the
  session's current view -- the view set of ``session.snapshot()``, leased for the task; refused while the
  view is still being computed; single-value documents: always the document), format assertion, the
  issues listed at most (``max_issues``) and per record (``max_per_record``);
* "Tekshirish" runs ONE heavy task on the pinned document: the schema file is read and compiled in the
  worker, then :func:`jschema.validate` runs (progress in bytes, "To'xtatish" cancels). The issues are
  listed in a :class:`~jsonreshape.gui.vgrid.VirtualGrid` (``#record | path | message | keyword``; the
  member key next to the record number of keyed documents);
* activating an issue (double click / Enter / "Daraxtda ko'rsatish") selects and reveals its node:
  ``NodeRef(issue.record, issue.path)`` (:func:`issue_ref`); single-value documents use
  ``NodeRef(None, path)``; issues of the document itself (``record=None``) point into the wrapper of a
  wrapped document (``NodeRef(None, path)``); for the other kinds they concern the record container
  (``minItems``, ``uniqueItems``, ...), which has no node in the views -- the panel says so;
* "Xatoli yozuvlarni ko'rsatish": a copy of ``report.records`` (tagged with the report's edit-state gen,
  made in a silent task while the report is leased) goes to ``session.show_records(copy, label)``;
* "Eksport…": every issue (not only the listed ones) to CSV (UTF-8 with BOM) or JSON Lines
  (:func:`write_issues`: ``Issue.to_dict()``), produced by :func:`jschema.iter_issues` in a heavy task with
  the panel's current inputs on a document pinned when the export starts (it validates again, like the
  search panel's export); the source file, its data file and the schema file are refused as output, so is
  a file another tab shows with changes (review S3: a tab showing it unchanged is asked about and reloads
  the new file);
* the summary: records validated, invalid records, issues, the scope used, issues per keyword, the most
  frequent paths, document-level issues and the core's warnings (``report.warning_texts()``: records not
  validated, the issue list cut, ...).

Streaming: :func:`jschema.validate` has no per-issue hook today and :func:`jschema.iter_issues` reports
neither the records it skipped (over 8 MiB, unreadable, too deep) nor its warnings. Listing the issues
of ``iter_issues`` while computing the summary on the side would therefore hide skipped records -- a
schema check that silently says "valid". The panel runs ``validate()`` (exact report) and lists its issues
when it ends; when ``validate`` accepts an ``on_issues`` callback (requested from the jschema module: each
batch of collected issues, called in the worker thread) the panel passes one and the issues stream into
the grid while the validation runs (the first ``max_issues`` are listed; the report replaces them at the
end). The capability is detected at call time (``inspect.signature``).

Edits (docs/ARCHITECTURE.md §12.9): a validation reads the pinned state shown when it started. When the
document changes (``session.data_changed``) while it runs, it is stopped and its results dropped
("validate again"); after it ended, a change that moves records (``cs.remap`` / ``cs.reset``) drops the
results too (their record numbers no longer address the same records), any other change keeps the list
marked as outdated (record numbers still valid) but "show the invalid records" is refused (the set
belongs to the older state). A reopened file drops the results; a closed session releases them.

Threading: the GUI thread never reads the data or the schema file: schema checks, validations, exports,
generation and record-set copies run in tasks; workers never touch widgets (results come back through the
task callbacks / ``TaskContext.post``). Tasks of the panel belong to a private owner object, detached when
the panel shuts down (:meth:`ValidatePanel.shutdown`, also when the widget is destroyed); every report it
holds is closed (its RecordSet released) when it is replaced, dropped or the panel shuts down.

Integration API (round B): ``GenerateSchemaDialog(session, parent=None, *, ask_save_path=None)``,
``show_generate_dialog(session, parent=None, **kw)``; ``ValidatePanel(app, parent=None, *,
ask_open_path=None, ask_save_path=None)`` with ``set_session(session)``, ``set_schema_path(path)``,
``run_validation()``, ``shutdown()``; ``ValidateDialog(session, parent=None, *, schema_path=None, ...)``
and ``show_validate_dialog(session, parent=None, **kw)`` (one dialog per session, brought to the front
when it is open). The path askers are ``fn(parent, title, start_path, filters) -> str | None``.
"""

from __future__ import annotations

import csv
import inspect
import io
import logging
import os
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import codec, jschema
from jsonreshape.errors import Cancelled, JsonReshapeError, UserInputError
from jsonreshape.fileio import atomic_write, long_path
from jsonreshape.gui import copyfmt, fields, theme
from jsonreshape.gui.searchpanel import check_not_source, keep_for, open_target_check, reload_tabs, session_snapshot
from jsonreshape.gui.session import NodeRef
from jsonreshape.gui.vgrid import Cell, Column, VirtualGrid
from jsonreshape.gui.widgets import format_count, format_size, keep_alive, set_clipboard_text
from jsonreshape.i18n import t
from jsonreshape.recordset import RecordSet, RecordSetBuilder
from jsonreshape.schema import infer_schema
from jsonreshape.tasks import CancelToken, check, format_duration

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_ISSUES",
    "DEFAULT_PER_RECORD",
    "EXPORT_FIELDS",
    "EXPORT_FORMATS",
    "GEN_SOURCES",
    "SCOPES",
    "SOURCES",
    "GenerateOutcome",
    "GenerateSchemaDialog",
    "ValidateDialog",
    "ValidatePanel",
    "ask_open_file",
    "ask_save_file",
    "default_schema_path",
    "issue_ref",
    "show_generate_dialog",
    "show_validate_dialog",
    "write_issues",
]

SCOPES = ("auto", "record", "document")         # jschema.validate(scope=...)
SOURCES = ("all", "view")                       # the records validated
GEN_SOURCES = ("cached", "full", "sample")      # what a generated schema is made from
EXPORT_FORMATS = {".csv": "csv", ".jsonl": "jsonl", ".ndjson": "jsonl"}
EXPORT_FIELDS = ("record", "key", "path", "keyword", "schema_path", "message", "params")
DEFAULT_MAX_ISSUES = 1000
DEFAULT_PER_RECORD = 10
MAX_ISSUES_LIMIT = 100_000      # issues kept in memory at most (like the search panel's MAX_HITS; GUI budget §11.0)
PER_RECORD_LIMIT = 10_000
SCHEMA_CHECK_DELAY_MS = 300     # the schema file is checked this long after the last edit of its path
SUMMARY_ITEMS = 6               # keywords / paths named in the summary at most
CHECK_EVERY = 256               # issues written between cancellation checks (export)
STATE_KEY = "validate_last"     # GuiState: the schema file and the options of the panel
GEN_STATE_KEY = "schema_gen_last"   # GuiState: the options of the generate dialog

PathAsker = Callable[[Any, str, str, str], "str | None"]   # (parent, title, start path, filters) -> path

# single-character stand-ins: one line per cell
_DISPLAY_MAP = {ord("\n"): "↵", ord("\r"): "↵", ord("\t"): "→"}
_DISPLAY_MAP.update({c: "·" for c in range(0x20) if c not in (9, 10, 13)})


# ==================================================================================
# helpers
# ==================================================================================
def ask_save_file(parent: Any, title: str, start: str, filters: str) -> str | None:
    """The default save-file asker (a native dialog)."""
    path, _selected = QFileDialog.getSaveFileName(parent, title, start, filters)
    return path or None


def ask_open_file(parent: Any, title: str, start: str, filters: str) -> str | None:
    """The default open-file asker (a native dialog)."""
    path, _selected = QFileDialog.getOpenFileName(parent, title, start, filters)
    return path or None


def _alive(obj: Any) -> bool:
    try:
        return obj is not None and bool(shiboken6.isValid(obj))
    except Exception:  # pragma: no cover - shiboken always available with PySide6
        return False


class _TaskOwner:
    """A plain owner object for the tasks of a widget (detached when the widget goes away)."""

    __slots__ = ("__weakref__",)


def _detach_on_destroy(widget: QObject, tasks: Any, owner: _TaskOwner) -> None:
    """Detach ``owner``'s tasks when ``widget`` is destroyed (the connected function keeps no
    reference to the widget)."""
    def detach(*_args: Any) -> None:
        try:
            tasks.detach(owner)
        except Exception:  # pragma: no cover - the manager is gone at exit
            log.debug("detach failed", exc_info=True)

    widget.destroyed.connect(detach)


def _display(text: Any) -> str:
    """``text`` on one line (control characters as visible symbols, lone surrogates escaped)."""
    return copyfmt.escape_surrogates(str(text).translate(_DISPLAY_MAP))


def _error_text(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    if isinstance(exc, JsonReshapeError):
        return exc.message
    return t("gui.widgets.error.unexpected")


def _status(app: Any, text: str) -> None:
    show = getattr(app, "show_status", None)
    if callable(show):
        show(text, 8000)


def _doc_bytes(doc: Any) -> int:
    """Bytes of the (effective) data of ``doc``: the progress total of a pass over it."""
    try:
        if getattr(doc, "edited", False):
            return max(1, int(doc.effective_bytes()))
        return max(1, int(doc.data_size) - int(doc.info.data_start))
    except Exception:  # noqa: BLE001 - a progress total only
        return 1


def default_schema_path(doc_path: str | os.PathLike[str]) -> str:
    """``<stem>.schema.json`` beside the document (``data.json.gz`` -> ``data.schema.json``)."""
    p = Path(os.fspath(doc_path))
    name = p.name
    if name.lower().endswith(".gz"):
        name = name[:-3]
    stem = os.path.splitext(name)[0] or name
    return str(p.with_name(f"{stem}.schema.json"))


def _streaming_validate() -> bool:
    """``jschema.validate`` accepts an ``on_issues`` callback (see the module docstring)."""
    try:
        return "on_issues" in inspect.signature(jschema.validate).parameters
    except (TypeError, ValueError):
        return False


def issue_ref(session: Any, issue: Any) -> NodeRef | None:
    """The node an issue points to (for ``session.select(ref, reveal=True)``), or ``None`` when it
    concerns the record container of the document (no node in the views)."""
    doc = getattr(session, "doc", None)
    kind = getattr(doc, "kind", None)
    path = tuple(issue.path)
    if kind == "single":
        return NodeRef(None, path)
    if issue.record is None:
        if kind == "wrapped" or path:
            return NodeRef(None, path)
        return None
    return NodeRef(int(issue.record), path)


def _record_text(issue: Any) -> str:
    if issue.record is None:
        return t("gui.validatepanel.document")
    text = f"#{issue.record}"
    if issue.key is not None:
        text += " · " + _display(issue.key)
    return text


def write_issues(issues: Iterable[Any], path: str | os.PathLike[str], fmt: str = "csv", *,
                 cancel: CancelToken | None = None) -> int:
    """Write every issue of ``issues`` (``jschema.Issue``; e.g. :func:`jschema.iter_issues`) to ``path``
    atomically: ``csv`` (UTF-8 with BOM; columns :data:`EXPORT_FIELDS`, ``params`` as JSON text) or
    ``jsonl`` (``Issue.to_dict()`` per line, exact numbers). Returns the number written."""
    fmt = (fmt or "csv").lower().lstrip(".")
    if fmt not in ("csv", "jsonl"):
        raise UserInputError("gui.validatepanel.export.bad_format", name=os.path.basename(os.fspath(path)))
    count = 0
    with atomic_write(path) as f:
        if fmt == "csv":
            text = io.TextIOWrapper(f, encoding="utf-8-sig", newline="")
            writer = csv.writer(text)
            writer.writerow(EXPORT_FIELDS)
            for issue in issues:
                count += 1
                if not count % CHECK_EVERY:
                    check(cancel)
                d = issue.to_dict()
                writer.writerow([
                    "" if d["record"] is None else d["record"],
                    "" if d["key"] is None else copyfmt.escape_surrogates(d["key"]),
                    copyfmt.escape_surrogates(d["path"]),
                    d["keyword"],
                    copyfmt.escape_surrogates(d["schema_path"]),
                    copyfmt.escape_surrogates(d["message"]),
                    copyfmt.escape_surrogates(codec.dumps(d["params"]).decode("utf-8", "replace")),
                ])
            text.flush()
            text.detach()
        else:
            for issue in issues:
                count += 1
                if not count % CHECK_EVERY:
                    check(cancel)
                f.write(codec.dumps(issue.to_dict()) + b"\n")
    return count


class _Lease:
    """An object with ``close()`` shared by its owner and background tasks: closed once the owner
    released it and no task uses it any more (GUI thread only)."""

    def __init__(self, obj: Any) -> None:
        self.obj = obj
        self.users = 0
        self.released = False

    def acquire(self) -> Any:
        self.users += 1
        return self.obj

    def done_using(self) -> None:
        self.users -= 1
        if self.released and self.users <= 0:
            self._close()

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.users <= 0:
            self._close()

    def _close(self) -> None:
        try:
            self.obj.close()
        except Exception:  # pragma: no cover - close never raises
            log.debug("closing %r failed", self.obj, exc_info=True)


def _copy_records(rs: RecordSet, gen: Any, label: str, cancel: CancelToken | None) -> RecordSet:
    """A copy of ``rs`` (worker thread) tagged with ``gen`` (``show_records`` refuses another state)."""
    builder = RecordSetBuilder("invalid", label, gen=gen)
    try:
        for chunk in rs.iter_chunks():
            check(cancel)
            builder.extend(chunk)
        return builder.finish()
    except BaseException:
        builder.abort()
        raise


# ==================================================================================
# schema generation
# ==================================================================================
@dataclass
class GenerateOutcome:
    """What a generation wrote: the file, its size, the number of fields, the records the schema is
    based on (``scanned`` of ``total_records``; ``complete``: every record), the data source
    (:data:`GEN_SOURCES`), the options and the time."""

    path: str
    size: int
    fields: int
    scanned: int
    total_records: int
    complete: bool
    source: str
    document: bool
    strict: bool
    elapsed: float


class _GenJob:
    """One generation (plain Python: the task's owner and callbacks; the dialog may be gone)."""

    def __init__(self, dialog: GenerateSchemaDialog, app: Any, path: str) -> None:
        self._dialog = weakref.ref(dialog)
        self.app = app
        self.path = path
        self.task: Any = None
        self.state = "running"
        self.outcome: GenerateOutcome | None = None
        self.error: BaseException | None = None
        self.reload: list = []      # weak refs: tabs that showed the output file (reopened when it was written)

    def dialog(self) -> GenerateSchemaDialog | None:
        d = self._dialog()
        return d if _alive(d) else None

    def done(self, outcome: GenerateOutcome) -> None:
        self.state = "done"
        self.outcome = outcome
        reload, self.reload = self.reload, []
        reload_tabs(reload)
        _status(self.app, t("gui.validatepanel.gen.status", path=outcome.path))
        d = self.dialog()
        if d is not None:
            d._job_done(self, outcome)

    def failed(self, exc: BaseException) -> None:
        self.state = "failed"
        self.error = exc
        d = self.dialog()
        if d is not None and d.isVisible():
            d._job_failed(self, exc)
            return
        self.app.report_error(exc, d)
        if d is not None:
            d._job_failed(self, exc)

    def cancelled(self) -> None:
        self.state = "cancelled"
        d = self.dialog()
        if d is not None:
            d._job_cancelled(self)


class GenerateSchemaDialog(QDialog):
    """Generate a JSON Schema of a session's document and write it to a file (module docstring).

    Signals: ``generated(outcome)`` (:class:`GenerateOutcome`), ``generation_failed(exc)``."""

    generated = Signal(object)
    generation_failed = Signal(object)

    _OPEN: weakref.WeakKeyDictionary[Any, GenerateSchemaDialog] = weakref.WeakKeyDictionary()

    def __init__(self, session: Any, parent: QWidget | None = None, *, ask_save_path: PathAsker | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.app = session.app
        self._ask_save = ask_save_path or ask_save_file
        self.job: _GenJob | None = None
        self.task: Any = None
        self.outcome: GenerateOutcome | None = None
        self.error: BaseException | None = None
        self._queued_shown = False
        self._wanted_source: str | None = None
        self.setObjectName("generate_schema_dialog")
        self.setWindowTitle(t("gui.validatepanel.gen.title", name=session.name))
        self.setSizeGripEnabled(True)
        self._build()
        self._load_defaults()
        self.refresh_sources()
        self._refresh()
        self.app.tasks.task_updated.connect(self._on_task_updated)
        session.state_changed.connect(self._on_session_state)
        data_changed = getattr(session, "data_changed", None)
        if data_changed is not None and hasattr(data_changed, "connect"):
            data_changed.connect(self._on_session_data)
        theme.notifier().theme_changed.connect(self._style_labels)

    # ------------------------------------------------------------------ construction
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        intro = QLabel(t("gui.validatepanel.gen.intro"), self)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        src_box = self.source_box = QGroupBox(t("gui.validatepanel.gen.source"), self)
        src_layout = QVBoxLayout(src_box)
        self.source_group = QButtonGroup(self)
        self.source_buttons: dict[str, QRadioButton] = {}
        for i, key in enumerate(GEN_SOURCES):
            button = QRadioButton(src_box)
            button.setObjectName(f"source_{key}")
            self.source_group.addButton(button, i)
            self.source_buttons[key] = button
            src_layout.addWidget(button)
        self.source_buttons["full"].setText(t("gui.validatepanel.gen.source.full"))
        self.source_buttons["full"].setToolTip(t("gui.validatepanel.gen.source.full.tip"))
        self.sample_warning = QLabel(t("gui.validatepanel.gen.sample_warning"), src_box)
        self.sample_warning.setObjectName("sample_warning")
        self.sample_warning.setWordWrap(True)
        self.sample_warning.setVisible(False)
        src_layout.addWidget(self.sample_warning)
        self.source_group.idToggled.connect(self._on_source_toggled)
        layout.addWidget(src_box)

        level_box = self.level_box = QGroupBox(t("gui.validatepanel.gen.level"), self)
        level_layout = QVBoxLayout(level_box)
        self.level_group = QButtonGroup(self)
        self.document_button = QRadioButton(t("gui.validatepanel.gen.level.document"), level_box)
        self.document_button.setToolTip(t("gui.validatepanel.gen.level.document.tip"))
        self.record_button = QRadioButton(t("gui.validatepanel.gen.level.record"), level_box)
        self.record_button.setToolTip(t("gui.validatepanel.gen.level.record.tip"))
        self.level_group.addButton(self.document_button, 0)
        self.level_group.addButton(self.record_button, 1)
        self.document_button.setChecked(True)
        level_layout.addWidget(self.document_button)
        level_layout.addWidget(self.record_button)
        layout.addWidget(level_box)

        self.strict_check = QCheckBox(t("gui.validatepanel.gen.strict"), self)
        self.strict_check.setToolTip(t("gui.validatepanel.gen.strict.tip"))
        layout.addWidget(self.strict_check)

        title_row = QHBoxLayout()
        title_label = QLabel(t("gui.validatepanel.gen.title_label"), self)
        self.title_edit = QLineEdit(self)
        self.title_edit.setObjectName("title_edit")
        self.title_edit.setText(self.session.name)
        self.title_edit.setClearButtonEnabled(True)
        title_label.setBuddy(self.title_edit)
        title_row.addWidget(title_label)
        title_row.addWidget(self.title_edit, 1)
        layout.addLayout(title_row)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(8)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        self.status_label = QLabel(self)
        self.status_label.setObjectName("status_label")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)
        self.error_label = QLabel(self)
        self.error_label.setObjectName("error_label")
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        self.save_button = QPushButton(t("gui.validatepanel.gen.save"), self)
        self.save_button.setToolTip(t("gui.validatepanel.gen.save.tip"))
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(lambda: self.generate())
        buttons.addWidget(self.save_button)
        self.stop_button = QPushButton(t("gui.validatepanel.gen.stop"), self)
        self.stop_button.setAutoDefault(False)
        self.stop_button.clicked.connect(self.cancel)
        self.stop_button.setVisible(False)
        buttons.addWidget(self.stop_button)
        self.open_button = QPushButton(t("gui.validatepanel.gen.open"), self)
        self.open_button.setToolTip(t("gui.validatepanel.gen.open.tip"))
        self.open_button.setAutoDefault(False)
        self.open_button.clicked.connect(self.open_result)
        self.open_button.setVisible(False)
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        close = QPushButton(t("gui.common.close"), self)
        close.setAutoDefault(False)
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._style_labels()
        self.resize(560, self.sizeHint().height())

    def _style_labels(self) -> None:
        if not _alive(self):
            return
        self.sample_warning.setStyleSheet(f"color: {theme.color('warn').name()};")
        self.error_label.setStyleSheet(f"color: {theme.color('danger').name()};")

    def _load_defaults(self) -> None:
        state = getattr(self.app, "state", None)
        data = state.get(GEN_STATE_KEY) if state is not None else None
        if not isinstance(data, dict):
            return
        if isinstance(data.get("strict"), bool):
            self.strict_check.setChecked(data["strict"])
        if isinstance(data.get("document"), bool):
            self.set_document_level(data["document"])
        if data.get("source") in GEN_SOURCES:
            self._wanted_source = str(data["source"])

    def _remember(self) -> None:
        state = getattr(self.app, "state", None)
        if state is None:
            return
        try:
            state.set(GEN_STATE_KEY, {"strict": self.strict(), "document": self.document_level(),
                                      "source": self.source_choice()})
        except (TypeError, ValueError):  # pragma: no cover - plain JSON values
            return
        save = getattr(self.app, "save_state", None)
        if callable(save):
            save()

    # ------------------------------------------------------------------ inputs
    def source_choice(self) -> str:
        for key, button in self.source_buttons.items():
            if button.isChecked():
                return key
        return "full"

    def set_source_choice(self, choice: str) -> None:
        button = self.source_buttons.get(choice)
        if button is not None and button.isEnabled():
            button.setChecked(True)

    def document_level(self) -> bool:
        return self.document_button.isChecked()

    def set_document_level(self, on: bool) -> None:
        (self.document_button if on else self.record_button).setChecked(True)

    def strict(self) -> bool:
        return self.strict_check.isChecked()

    def set_strict(self, on: bool) -> None:
        self.strict_check.setChecked(bool(on))

    def title_text(self) -> str:
        return self.title_edit.text().strip()

    def set_title_text(self, text: str) -> None:
        self.title_edit.setText(str(text))

    def _fresh_schema(self, *, full: bool) -> Any:
        """The cached schema of the session when it belongs to the data shown (``full``: only a full
        analysis), else ``None``."""
        session = self.session
        if session is None or getattr(session, "closed", False) or session.doc is None:
            return None
        if fields.is_stale(session):
            return None
        return fields.cached_schema(session, full=full)

    def refresh_sources(self) -> None:
        """Update the source choices from the shared schema cache (called when the dialog is shown,
        before a generation and when the document changes)."""
        cached = self._fresh_schema(full=True)
        button = self.source_buttons["cached"]
        if cached is not None:
            button.setText(t("gui.validatepanel.gen.source.cached", count=format_count(cached.scanned)))
            button.setToolTip(t("gui.validatepanel.gen.source.cached.tip"))
            button.setEnabled(True)
        else:
            if button.isChecked():
                self.source_buttons["full"].setChecked(True)
            button.setText(t("gui.validatepanel.gen.source.cached_none"))
            button.setToolTip(t("gui.validatepanel.gen.source.cached.tip_none"))
            button.setEnabled(False)
        sample = max(1, int(getattr(getattr(self.app, "settings", None), "sample_size", 1000) or 1000))
        self.source_buttons["sample"].setText(t("gui.validatepanel.gen.source.sample", count=format_count(sample)))
        self.source_buttons["sample"].setToolTip(t("gui.validatepanel.gen.source.sample.tip"))
        if self.source_group.checkedButton() is None:
            wanted = self._wanted_source
            if wanted in ("sample", "full"):
                self.source_buttons[wanted].setChecked(True)
            elif cached is not None:
                button.setChecked(True)
            else:
                self.source_buttons["full"].setChecked(True)
        self._on_source_toggled()

    def _on_source_toggled(self, *_args: Any) -> None:
        self.sample_warning.setVisible(self.source_choice() == "sample")

    # ------------------------------------------------------------------ running
    @property
    def running(self) -> bool:
        return self.task is not None and self.task.state == "running"

    def _default_path(self) -> str:
        session = self.session
        return default_schema_path(session.path)

    def _set_error(self, text: str | None) -> None:
        self.error_label.setText(copyfmt.escape_surrogates(text or ""))
        self.error_label.setVisible(bool(text))

    def error_text(self) -> str:
        return self.error_label.text() if not self.error_label.isHidden() else ""

    def status_text(self) -> str:
        return self.status_label.text()

    def generate(self, path: str | os.PathLike[str] | None = None) -> Any:
        """Ask for the file (when ``path`` is ``None``) and start the generation task; returns the task
        (``None`` when refused or cancelled -- the reason is shown)."""
        if self.running:
            return None
        session = self.session
        if session is None or getattr(session, "closed", False) or session.doc is None:
            self._set_error(t("gui.validatepanel.gen.no_document"))
            return None
        if path is None:
            path = self._ask_save(self, t("gui.validatepanel.gen.save_title"), self._default_path(),
                                  t("gui.validatepanel.schema.filter"))
            if not path:
                return None
        out = os.path.abspath(os.fspath(path))
        if not os.path.splitext(out)[1]:
            out += ".json"
        live = session.doc
        try:
            check_not_source(out, (live.source_path, live.data_path, session.path))
            if os.path.isdir(long_path(out)):
                raise UserInputError("fileio.error.is_directory", path=out)
        except UserInputError as exc:
            text = exc.message
            if exc.key == "writer.error.same_file":
                text += " " + t("gui.validatepanel.same_file.hint")
            self._set_error(text)
            return None
        self._set_error(None)
        # a file another tab shows: refused while it has changes, else asked; that tab reloads it (review S3)
        reload = open_target_check(self, out, exclude=session, on_refused=self._set_error)
        if reload is None:
            return None
        self.refresh_sources()
        choice = self.source_choice()
        schema: Any = None
        sample: int | None = None
        if choice == "cached":
            schema = self._fresh_schema(full=True)
            if schema is None:      # pragma: no cover - refresh_sources switched to "full"
                choice = "full"
        elif choice == "sample":
            schema = self._fresh_schema(full=False)
            if schema is None:
                sample = max(1, int(self.app.settings.sample_size))
        document = self.document_level()
        strict = self.strict()
        title = self.title_text()
        doc, _view = session_snapshot(session)
        gen = int(getattr(session, "data_gen", 0) or 0)     # the data generation of the pinned state
        name = session.name
        file_name = os.path.basename(out)

        def work(ctx: Any) -> GenerateOutcome:
            started = time.perf_counter()
            progress = ctx.progress
            progress.set_total(_doc_bytes(doc))
            sch = schema
            p_gen = progress if strict else None
            if sch is None:
                progress.set_message(t("gui.validatepanel.gen.progress.scan", name=name))
                share = 0.5 if strict else 1.0
                sch = infer_schema(doc, None, sample=sample, progress=progress.child(0.0, share), cancel=ctx.cancel)
                ctx.post(sch)       # the shared schema cache gets the scan (on_item, GUI thread)
                p_gen = progress.child(share, 1.0) if strict else None
            if strict:
                progress.set_message(t("gui.validatepanel.gen.progress.strict", name=name))
            value = jschema.generate_schema(sch, doc=doc, document=document, strict=strict, title=title,
                                            progress=p_gen, cancel=ctx.cancel)
            data = jschema.dump_schema(value)
            check(ctx.cancel)
            progress.set_message(t("gui.validatepanel.gen.progress.write", name=file_name))
            with atomic_write(out) as f:
                f.write(data)
            progress.finish()
            return GenerateOutcome(path=out, size=len(data), fields=len(sch.fields()), scanned=int(sch.scanned),
                                   total_records=int(sch.total_records), complete=bool(sch.complete), source=choice,
                                   document=document, strict=strict, elapsed=time.perf_counter() - started)

        job = _GenJob(self, self.app, out)
        job.reload = reload
        heavy = schema is None or strict
        job.task = self.app.tasks.run(work, title=t("gui.validatepanel.gen.task", name=file_name), kind="schema",
                                      owner=job, heavy=heavy, on_done=job.done, on_error=job.failed,
                                      on_cancel=job.cancelled,
                                      on_item=lambda s_: fields.store(session, s_, full=s_.complete, gen=gen, doc=doc))
        keep_for(session, job.task, doc=doc)
        self.job = job
        self.task = job.task
        self.outcome = None
        self.error = None
        self._queued_shown = False
        self._remember()
        self.status_label.setText(t("gui.validatepanel.gen.running"))
        self._refresh()
        return job.task

    def cancel(self) -> None:
        """Stop the running generation (nothing is written)."""
        if self.running:
            self.task.cancel()

    def open_result(self) -> bool:
        """Open the written schema file in a new tab (``app.request_open``)."""
        if self.outcome is None:
            return False
        request = getattr(self.app, "request_open", None)
        if not callable(request):
            return False
        request(self.outcome.path, {})
        return True

    # ------------------------------------------------------------------ job callbacks
    def _job_done(self, job: _GenJob, outcome: GenerateOutcome) -> None:
        if job is not self.job:
            return
        self.task = None
        self.outcome = outcome
        text = t("gui.validatepanel.gen.done", path=outcome.path, size=format_size(outcome.size),
                 fields=format_count(outcome.fields), records=format_count(outcome.scanned))
        if not outcome.complete:
            text += "\n" + t("gui.validatepanel.gen.done_sample", records=format_count(outcome.scanned),
                             total=format_count(outcome.total_records))
        self.status_label.setText(copyfmt.escape_surrogates(text))
        self._refresh()
        self.generated.emit(outcome)

    def _job_failed(self, job: _GenJob, exc: BaseException) -> None:
        if job is not self.job:
            return
        self.task = None
        self.error = exc
        self.status_label.setText("")
        self._set_error(t("gui.validatepanel.gen.failed", error=_error_text(exc)))
        self._refresh()
        self.generation_failed.emit(exc)

    def _job_cancelled(self, job: _GenJob) -> None:
        if job is not self.job:
            return
        self.task = None
        self.status_label.setText(t("gui.validatepanel.gen.cancelled"))
        self._refresh()

    def _on_task_updated(self, task: Any) -> None:
        if task is None or task is not self.task or task.state != "running":
            return
        if getattr(task, "queued", False):
            self._queued_shown = True
            self.progress_bar.setRange(0, 0)
            self.status_label.setText(t("gui.tasks.queued"))
            return
        if self._queued_shown:
            self._queued_shown = False
            self.status_label.setText(t("gui.validatepanel.gen.running"))
        info = task.info()
        if info is None or info.fraction is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(max(0.0, min(1.0, info.fraction)) * 1000))
        if info is not None and info.message:
            self.status_label.setText(copyfmt.escape_surrogates(info.message))

    def _refresh(self) -> None:
        running = self.running
        has_doc = self.session is not None and not getattr(self.session, "closed", False) \
            and self.session.doc is not None
        self.save_button.setEnabled(has_doc and not running)
        self.stop_button.setVisible(running)
        self.progress_bar.setVisible(running)
        if running:
            self.progress_bar.setRange(0, 0)
        self.open_button.setVisible(self.outcome is not None and not running)
        self.source_box.setEnabled(not running)
        self.level_box.setEnabled(not running)
        self.strict_check.setEnabled(not running)
        self.title_edit.setEnabled(not running)

    # ------------------------------------------------------------------ session
    def _on_session_state(self, value: str) -> None:
        if value == "closed":
            self.session = None
            self._refresh()
            self.close()
        elif value in ("ready", "opening"):
            self.refresh_sources()
            self._refresh()

    def _on_session_data(self, _cs: Any) -> None:
        self.refresh_sources()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt name
        super().showEvent(event)
        if self.session is not None and not getattr(self.session, "closed", False):
            self.refresh_sources()

    @classmethod
    def show_for(cls, session: Any, parent: QWidget | None = None, **kwargs: Any) -> GenerateSchemaDialog:
        """The generate dialog of ``session`` (an open one is brought to the front), shown non-modal."""
        dialog = cls._OPEN.get(session)
        if dialog is None or not _alive(dialog):
            dialog = cls(session, parent, **kwargs)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            if parent is None:
                keep_alive(dialog)
            cls._OPEN[session] = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog


def show_generate_dialog(session: Any, parent: QWidget | None = None, **kwargs: Any) -> GenerateSchemaDialog:
    """Tools ▸ "Schema yaratish…": :meth:`GenerateSchemaDialog.show_for`."""
    return GenerateSchemaDialog.show_for(session, parent, **kwargs)


# ==================================================================================
# validation
# ==================================================================================
class _ValidateState:
    """The validation results of one session."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.gen = 0
        self.status = "idle"            # idle | running | done | cancelled | failed | stale
        self.task: Any = None
        self.opts: dict[str, Any] | None = None
        self.issues: list[Any] = []
        self.live_count = 0              # issues streamed so far (running)
        self.report: Any = None          # jschema.ValidationReport of a finished run
        self.lease: _Lease | None = None
        self.error: BaseException | None = None
        self.message = ""
        self.outdated = False
        self.started = 0.0
        self.elapsed = 0.0
        self.data_gen: Any = None
        self.view_used = False
        self.source_label = ""
        self.schema_name = ""

    @property
    def running(self) -> bool:
        return self.status == "running"

    def release(self) -> None:
        """Forget the report (its RecordSet is closed once no task uses it)."""
        if self.lease is not None:
            self.lease.release()
        self.lease = None
        self.report = None


class _IssueSource:
    """GridSource over the listed issues of a :class:`_ValidateState`."""

    def __init__(self) -> None:
        self.state: _ValidateState | None = None

    def row_count(self) -> int:
        return len(self.state.issues) if self.state is not None else 0

    def cell(self, row: int, column: Column) -> Cell | None:
        st = self.state
        if st is None or not 0 <= row < len(st.issues):
            return None
        issue = st.issues[row]
        cid = column.id
        if cid == "record":
            return Cell(_record_text(issue), role="muted", italic=issue.record is None)
        if cid == "path":
            return Cell(_display(issue.path_text), role="key")
        if cid == "message":
            message = _display(issue.message)
            tip = message + "\n" + t("gui.validatepanel.schema_path_tip", pointer=_display(issue.schema_path or "#"))
            return Cell(message, tooltip=tip)
        return Cell(issue.keyword, role="muted")

    def rows_shown(self, first: int, last: int) -> None:
        return None

    def row_label(self, row: int) -> str | None:
        return None


class _KeyFilter(QObject):
    """Enter in the schema field runs the validation, Escape stops a running one."""

    def __init__(self, panel: ValidatePanel) -> None:
        super().__init__(panel)
        self._panel = panel

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt name
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._panel.run_validation()
                return True
            if key == Qt.Key.Key_Escape and self._panel.is_running():
                self._panel.cancel_validation()
                return True
        return False


class ValidatePanel(QWidget):
    """Validate the current session's document against a JSON Schema file (module docstring).

    Signals: ``validation_started(session)``, ``validation_finished(session, status)`` (``done`` |
    ``cancelled`` | ``failed``), ``issues_changed(session)``, ``schema_checked(ok, text)``,
    ``export_finished(path, count)``, ``records_opened(session)``."""

    validation_started = Signal(object)
    validation_finished = Signal(object, str)
    issues_changed = Signal(object)
    schema_checked = Signal(bool, str)
    export_finished = Signal(str, object)
    records_opened = Signal(object)

    def __init__(self, app: Any, parent: QWidget | None = None, *, ask_open_path: PathAsker | None = None,
                 ask_save_path: PathAsker | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self._ask_open = ask_open_path or ask_open_file
        self._ask_save = ask_save_path or ask_save_file
        self._owner = _TaskOwner()
        _detach_on_destroy(self, app.tasks, self._owner)
        self._session: Any = None
        self._states: dict[int, _ValidateState] = {}
        self._source = _IssueSource()
        self._schema_gen = 0
        self._schema_task: Any = None
        self._export_task: Any = None
        self._error_shown = False
        self._shut = False
        self._schema_timer = QTimer(self)
        self._schema_timer.setSingleShot(True)
        self._schema_timer.setInterval(SCHEMA_CHECK_DELAY_MS)
        self._schema_timer.timeout.connect(self.check_schema)
        self._build()
        self.grid.set_source(self._source)
        self._load_defaults()
        app.tasks.task_updated.connect(self._on_task_updated)
        theme.notifier().theme_changed.connect(self._style_labels)
        self._refresh()

    # ================================================================================
    # construction
    # ================================================================================
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        row = QHBoxLayout()
        schema_label = QLabel(t("gui.validatepanel.schema.label"), self)
        row.addWidget(schema_label)
        self.schema_edit = QLineEdit(self)
        self.schema_edit.setObjectName("schema_edit")
        self.schema_edit.setPlaceholderText(t("gui.validatepanel.schema.placeholder"))
        self.schema_edit.setClearButtonEnabled(True)
        self.schema_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.schema_edit.textChanged.connect(self._on_schema_text)
        schema_label.setBuddy(self.schema_edit)
        row.addWidget(self.schema_edit, 1)
        self.browse_button = QPushButton(t("gui.validatepanel.schema.browse"), self)
        self.browse_button.setAutoDefault(False)
        self.browse_button.clicked.connect(self.choose_schema_file)
        row.addWidget(self.browse_button)
        layout.addLayout(row)
        self.schema_info = QLabel(self)
        self.schema_info.setObjectName("schema_info")
        self.schema_info.setWordWrap(True)
        self.schema_info.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.schema_info)

        opts = QHBoxLayout()
        opts.setSpacing(8)
        scope_label = QLabel(t("gui.validatepanel.scope.label"), self)
        opts.addWidget(scope_label)
        self.scope_combo = QComboBox(self)
        for scope in SCOPES:
            self.scope_combo.addItem(t(f"gui.validatepanel.scope.{scope}"), scope)
        self.scope_combo.setToolTip(t("gui.validatepanel.scope.tip"))
        scope_label.setBuddy(self.scope_combo)
        opts.addWidget(self.scope_combo)
        source_label = QLabel(t("gui.validatepanel.source.label"), self)
        opts.addWidget(source_label)
        self.source_combo = QComboBox(self)
        self.source_combo.addItem(t("gui.validatepanel.source.all"), "all")
        self.source_combo.addItem(t("gui.validatepanel.source.view"), "view")
        self.source_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.source_combo.setMinimumContentsLength(12)
        source_label.setBuddy(self.source_combo)
        opts.addWidget(self.source_combo, 1)
        layout.addLayout(opts)

        opts2 = QHBoxLayout()
        opts2.setSpacing(8)
        self.formats_check = QCheckBox(t("gui.validatepanel.formats"), self)
        self.formats_check.setToolTip(t("gui.validatepanel.formats.tip"))
        opts2.addWidget(self.formats_check)
        max_label = QLabel(t("gui.validatepanel.max_issues"), self)
        opts2.addWidget(max_label)
        self.max_spin = QSpinBox(self)
        self.max_spin.setRange(1, MAX_ISSUES_LIMIT)
        self.max_spin.setValue(DEFAULT_MAX_ISSUES)
        self.max_spin.setToolTip(t("gui.validatepanel.max_issues.tip"))
        max_label.setBuddy(self.max_spin)
        opts2.addWidget(self.max_spin)
        per_label = QLabel(t("gui.validatepanel.per_record"), self)
        opts2.addWidget(per_label)
        self.per_record_spin = QSpinBox(self)
        self.per_record_spin.setRange(1, PER_RECORD_LIMIT)
        self.per_record_spin.setValue(DEFAULT_PER_RECORD)
        self.per_record_spin.setToolTip(t("gui.validatepanel.per_record.tip"))
        per_label.setBuddy(self.per_record_spin)
        opts2.addWidget(self.per_record_spin)
        opts2.addStretch(1)
        self.run_button = QPushButton(t("gui.validatepanel.run"), self)
        self.run_button.setToolTip(t("gui.validatepanel.run.tip"))
        self.run_button.setAutoDefault(False)
        self.run_button.clicked.connect(self.run_validation)
        opts2.addWidget(self.run_button)
        self.cancel_button = QPushButton(t("gui.validatepanel.cancel"), self)
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.cancel_validation)
        self.cancel_button.setVisible(False)
        opts2.addWidget(self.cancel_button)
        layout.addLayout(opts2)

        self._key_filter = _KeyFilter(self)
        self.schema_edit.installEventFilter(self._key_filter)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(6)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        self.error_label = QLabel(self)
        self.error_label.setObjectName("error_label")
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)
        self.summary_label = QLabel(self)
        self.summary_label.setObjectName("summary_label")
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary_label)
        self.warnings_label = QLabel(self)
        self.warnings_label.setObjectName("warnings_label")
        self.warnings_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.warnings_label.setVisible(False)
        layout.addWidget(self.warnings_label)
        self.note_label = QLabel(self)
        self.note_label.setObjectName("note_label")
        self.note_label.setTextFormat(Qt.TextFormat.PlainText)
        self.note_label.setWordWrap(True)
        self.note_label.setVisible(False)
        layout.addWidget(self.note_label)

        self.grid = VirtualGrid(self)
        self.grid.setObjectName("issues_grid")
        self.grid.set_selection_mode("rows")
        self.grid.set_columns([
            Column("record", t("gui.validatepanel.col.record"), width=110),
            Column("path", t("gui.validatepanel.col.path"), width=200),
            Column("message", t("gui.validatepanel.col.message"), width=420),
            Column("keyword", t("gui.validatepanel.col.keyword"), width=130),
        ])
        self.grid.set_placeholder_text(t("gui.validatepanel.empty"))
        self.grid.activated.connect(lambda row, _col: self.activate_issue(int(row)))
        self.grid.context_menu_requested.connect(self._context_menu)
        self.grid.set_key_handler(self._grid_key)
        layout.addWidget(self.grid, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.records_button = QPushButton(t("gui.validatepanel.records"), self)
        self.records_button.setAutoDefault(False)
        self.records_button.clicked.connect(self.show_invalid_records)
        bottom.addWidget(self.records_button)
        self.export_button = QPushButton(t("gui.validatepanel.export"), self)
        self.export_button.setToolTip(t("gui.validatepanel.export.tip"))
        self.export_button.setAutoDefault(False)
        self.export_button.clicked.connect(self.choose_export_file)
        bottom.addWidget(self.export_button)
        bottom.addStretch(1)
        layout.addLayout(bottom)
        self._style_labels()

    def _style_labels(self) -> None:
        if self._shut:
            return
        self.error_label.setStyleSheet(f"color: {theme.color('danger').name()};")
        self.warnings_label.setStyleSheet(f"color: {theme.color('warn').name()};")
        self.note_label.setStyleSheet(f"color: {theme.color('muted').name()};")
        self._color_schema_info()

    def _color_schema_info(self) -> None:
        bad = bool(self.schema_info.property("invalid"))
        role = "danger" if bad else "muted"
        self.schema_info.setStyleSheet(f"color: {theme.color(role).name()};")

    def _load_defaults(self) -> None:
        state = getattr(self.app, "state", None)
        data = state.get(STATE_KEY) if state is not None else None
        if not isinstance(data, dict):
            return
        if data.get("scope") in SCOPES:
            self.scope_combo.setCurrentIndex(SCOPES.index(data["scope"]))
        if data.get("source") in SOURCES:
            self.source_combo.setCurrentIndex(SOURCES.index(data["source"]))
        if isinstance(data.get("formats"), bool):
            self.formats_check.setChecked(data["formats"])
        for key, spin in (("max_issues", self.max_spin), ("per_record", self.per_record_spin)):
            value = data.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                spin.setValue(value)
        if isinstance(data.get("schema"), str) and data["schema"]:
            self.schema_edit.setText(data["schema"])

    def _remember(self) -> None:
        state = getattr(self.app, "state", None)
        if state is None:
            return
        opts = self.input_values()
        try:
            state.set(STATE_KEY, opts)
        except (TypeError, ValueError):  # pragma: no cover - plain JSON values
            return
        save = getattr(self.app, "save_state", None)
        if callable(save):
            save()

    # ================================================================================
    # sessions
    # ================================================================================
    def session(self) -> Any:
        return self._session

    def set_session(self, session: Any) -> None:
        """Show the validation results of ``session`` (``None``: no document)."""
        if session is not None and getattr(session, "closed", False):
            session = None
        self._session = session
        st = self._state_for(session) if session is not None else None
        self._source.state = st
        self._error_shown = False
        self._set_error(None)
        self._set_note(None)
        self.grid.reset()
        self._refresh()

    def _state_for(self, session: Any, create: bool = True) -> _ValidateState | None:
        st = self._states.get(id(session))
        if st is not None and st.session is not session:    # a recycled id
            st.release()
            del self._states[id(session)]
            st = None
        if st is None and create and session is not None:
            st = _ValidateState(session)
            self._states[id(session)] = st
            session.state_changed.connect(self._on_session_state)
            session.view_changed.connect(self._on_session_view)
            session.count_changed.connect(self._on_session_view)
            data_changed = getattr(session, "data_changed", None)
            if data_changed is not None and hasattr(data_changed, "connect"):
                data_changed.connect(self._on_session_data)
            busy = getattr(session, "view_busy_changed", None)
            if busy is not None and hasattr(busy, "connect"):
                busy.connect(self._on_session_busy)
        return st

    def state(self, session: Any = None) -> _ValidateState | None:
        """The result state of ``session`` (default: the current one)."""
        session = session if session is not None else self._session
        if session is None:
            return None
        return self._state_for(session, create=False)

    def _disconnect(self, session: Any) -> None:
        for name, slot in (("state_changed", self._on_session_state), ("view_changed", self._on_session_view),
                           ("count_changed", self._on_session_view), ("data_changed", self._on_session_data),
                           ("view_busy_changed", self._on_session_busy)):
            signal = getattr(session, name, None)
            if signal is None:
                continue
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _on_session_state(self, value: str) -> None:
        session = self.sender()
        st = self._states.get(id(session)) if session is not None else None
        if st is None or st.session is not session:
            return
        if value == "closed":
            self._stop(st)
            st.gen += 1
            st.release()
            del self._states[id(session)]
            if session is self._session:
                self.set_session(None)
            return
        if value == "opening" and (st.running or st.issues or st.report is not None):
            self._make_stale(st, t("gui.validatepanel.stale"))
        elif session is self._session:
            self._refresh()

    def _make_stale(self, st: _ValidateState, message: str) -> None:
        """Drop the results of ``st`` (a running validation is stopped)."""
        self._stop(st)
        st.gen += 1
        st.release()
        st.issues = []
        st.live_count = 0
        st.outdated = False
        st.status = "stale"
        st.message = message
        if st.session is self._session:
            self.grid.reset()
            self._refresh()
        self.issues_changed.emit(st.session)

    def _on_session_data(self, cs: Any) -> None:
        """An edit was applied (a ChangeSet): see the module docstring."""
        session = self.sender()
        st = self._states.get(id(session)) if session is not None else None
        if st is None or st.session is not session:
            return
        if st.running:
            self._make_stale(st, t("gui.validatepanel.stale_running"))
            return
        if st.report is None and not st.issues:
            if session is self._session:
                self._refresh()
            return
        if bool(getattr(cs, "reset", False)) or getattr(cs, "remap", None) is not None:
            self._make_stale(st, t("gui.validatepanel.stale_edit"))
            return
        st.outdated = True
        st.message = t("gui.validatepanel.outdated")
        if session is self._session:
            self._refresh()

    def _on_session_view(self) -> None:
        if self.sender() is self._session and self._session is not None:
            self._update_source_items()
            self._refresh_buttons()

    def _on_session_busy(self, _busy: bool) -> None:
        if self.sender() is self._session and self._session is not None:
            self._refresh_buttons()

    # ================================================================================
    # inputs
    # ================================================================================
    def schema_path(self) -> str:
        text = self.schema_edit.text().strip()
        if len(text) >= 2 and text[0] == text[-1] == '"':    # pasted "C:\...\x.json"
            text = text[1:-1].strip()
        return text

    def set_schema_path(self, path: str | os.PathLike[str] | None) -> None:
        """Use the schema file ``path`` (checked in the background)."""
        self.schema_edit.setText("" if path is None else os.fspath(path))
        self._schema_timer.stop()
        self.check_schema()

    def scope(self) -> str:
        return str(self.scope_combo.currentData() or "auto")

    def set_scope(self, scope: str) -> None:
        if scope in SCOPES:
            self.scope_combo.setCurrentIndex(SCOPES.index(scope))

    def source_choice(self) -> str:
        return str(self.source_combo.currentData() or "all")

    def set_source_choice(self, choice: str) -> None:
        if choice in SOURCES:
            self.source_combo.setCurrentIndex(SOURCES.index(choice))

    def formats(self) -> bool:
        return self.formats_check.isChecked()

    def set_formats(self, on: bool) -> None:
        self.formats_check.setChecked(bool(on))

    def max_issues(self) -> int:
        return int(self.max_spin.value())

    def set_max_issues(self, n: int) -> None:
        self.max_spin.setValue(int(n))

    def max_per_record(self) -> int:
        return int(self.per_record_spin.value())

    def set_max_per_record(self, n: int) -> None:
        self.per_record_spin.setValue(int(n))

    def input_values(self) -> dict[str, Any]:
        return {"schema": self.schema_path(), "scope": self.scope(), "source": self.source_choice(),
                "formats": self.formats(), "max_issues": self.max_issues(), "per_record": self.max_per_record()}

    def choose_schema_file(self) -> None:
        """"Tanlash…": ask for the schema file."""
        start = self.schema_path()
        if not start and self._session is not None:
            start = os.path.dirname(self._session.path)
        path = self._ask_open(self, t("gui.validatepanel.schema.browse_title"), start,
                              t("gui.validatepanel.schema.filter"))
        if path:
            self.set_schema_path(path)

    def _on_schema_text(self, _text: str) -> None:
        self._schema_gen += 1
        self.schema_info.setText("")
        self._schema_timer.start()
        self._refresh_buttons()

    def check_schema(self) -> Any:
        """Read and compile the schema file in a silent task; the result is shown under the field
        (``schema_checked(ok, text)``). Returns the task (``None`` without a path)."""
        self._schema_timer.stop()
        self._schema_gen += 1
        gen = self._schema_gen
        path = self.schema_path()
        if not path:
            self._show_schema_info("", False)
            return None
        self.schema_info.setText(t("gui.validatepanel.schema.checking"))

        def work(_ctx: Any) -> tuple[str, int]:
            value = jschema.load_schema(path)
            compiled = jschema.compile_schema(value)
            title = value.get("title") if isinstance(value, dict) else None
            return (title if isinstance(title, str) else ""), int(compiled.size)

        def done(result: tuple[str, int]) -> None:
            if gen != self._schema_gen or self._shut:
                return
            title, size = result
            if title:
                text = t("gui.validatepanel.schema.ok_title", title=_display(title), count=format_count(size))
            else:
                text = t("gui.validatepanel.schema.ok", count=format_count(size))
            self._show_schema_info(text, True)

        def failed(exc: BaseException) -> None:
            if gen != self._schema_gen or self._shut:
                return
            self._show_schema_info(_error_text(exc), False)

        self._schema_task = self.app.tasks.run(work, title=t("gui.validatepanel.task.schema"), kind="schema",
                                               owner=self._owner, silent=True, on_done=done, on_error=failed)
        return self._schema_task

    def _show_schema_info(self, text: str, ok: bool) -> None:
        self.schema_info.setText(copyfmt.escape_surrogates(text))
        self.schema_info.setProperty("invalid", bool(text) and not ok)
        self._color_schema_info()
        if text:
            self.schema_checked.emit(bool(ok), text)

    def schema_info_text(self) -> str:
        return self.schema_info.text()

    def _update_source_items(self) -> None:
        session = self._session
        doc = session.doc if session is not None and not getattr(session, "closed", False) else None
        single = doc is not None and doc.kind == "single"
        self.source_combo.setEnabled(doc is not None and not single)
        if single:
            self.source_combo.setToolTip(t("gui.validatepanel.source.single"))
            self.source_combo.setItemText(1, t("gui.validatepanel.source.view"))
            return
        if doc is None:
            self.source_combo.setToolTip("")
            self.source_combo.setItemText(1, t("gui.validatepanel.source.view"))
            return
        label = session.view_label()
        count = format_count(session.view_len())
        self.source_combo.setItemText(1, t("gui.validatepanel.source.view_label", label=label, count=count))
        self.source_combo.setToolTip(t("gui.validatepanel.source.view.tip", label=label))

    # ================================================================================
    # errors / display
    # ================================================================================
    def _set_error(self, text: str | None) -> None:
        self.error_label.setText(copyfmt.escape_surrogates(text or ""))
        self.error_label.setVisible(bool(text))
        self._error_shown = bool(text)

    def error_text(self) -> str:
        """The inline error shown (empty when none)."""
        return self.error_label.text() if not self.error_label.isHidden() else ""

    def summary_text(self) -> str:
        return self.summary_label.text()

    def _set_note(self, text: str | None) -> None:
        """A short muted note under the summary (e.g. why an issue has no node to show)."""
        self.note_label.setText(copyfmt.escape_surrogates(text or ""))
        self.note_label.setVisible(bool(text))

    def note_text(self) -> str:
        return self.note_label.text() if not self.note_label.isHidden() else ""

    def warnings_text(self) -> str:
        return self.warnings_label.text() if not self.warnings_label.isHidden() else ""

    def is_running(self, session: Any = None) -> bool:
        st = self.state(session)
        return st is not None and st.running

    def _refresh(self) -> None:
        if self._shut:
            return
        st = self.state()
        running = st is not None and st.running
        self.run_button.setVisible(not running)
        self.cancel_button.setVisible(running)
        self.cancel_button.setEnabled(running)
        self.progress_bar.setVisible(running)
        if running:
            self._update_progress(st.task)
        self._update_source_items()
        self.summary_label.setText(copyfmt.escape_surrogates(self._summary(st)))
        warnings = self._warnings(st)
        self.warnings_label.setText(copyfmt.escape_surrogates(warnings))
        self.warnings_label.setVisible(bool(warnings))
        if st is not None and st.status == "done" and not st.issues:
            self.grid.set_placeholder_text(t("gui.validatepanel.empty_ok"))
        else:
            self.grid.set_placeholder_text(t("gui.validatepanel.empty"))
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        if self._shut:
            return
        session = self._session
        has_doc = session is not None and not getattr(session, "closed", False) and session.doc is not None
        st = self.state()
        self.run_button.setEnabled(has_doc and bool(self.schema_path()))
        ok, why = self._can_show_records(st)
        self.records_button.setEnabled(ok)
        self.records_button.setToolTip(t("gui.validatepanel.records.tip") if ok else why)
        exporting = self._export_task is not None and self._export_task.state == "running"
        self.export_button.setEnabled(has_doc and bool(self.schema_path()) and not exporting)

    def _summary(self, st: _ValidateState | None) -> str:
        if self._session is None:
            return t("gui.validatepanel.no_document")
        if st is None:
            return ""
        parts: list[str] = []
        if st.status == "running":
            if st.live_count:
                parts.append(t("gui.validatepanel.live", issues=format_count(st.live_count)))
            else:
                parts.append(t("gui.validatepanel.running"))
        elif st.status == "done" and st.report is not None:
            rep = st.report
            secs = format_duration(rep.elapsed)
            if rep.ok:
                parts.append(t("gui.validatepanel.summary.ok", scanned=format_count(rep.scanned), seconds=secs))
            else:
                parts.append(t("gui.validatepanel.summary.bad", scanned=format_count(rep.scanned),
                               invalid=format_count(rep.invalid), issues=format_count(rep.issue_count), seconds=secs))
            scope_key = "gui.validatepanel.summary.scope.document" if rep.scope == "document" \
                else "gui.validatepanel.summary.scope.record"
            line = t(scope_key)
            if st.source_label:
                line += " " + st.source_label
            if rep.formats:
                line += " " + t("gui.validatepanel.summary.formats_on")
            parts.append(line)
            if rep.document_issues:
                parts.append(t("gui.validatepanel.summary.document", count=format_count(rep.document_issues)))
            if rep.counts:
                items = [t("gui.validatepanel.summary.item", name=k, count=format_count(v))
                         for k, v in list(rep.counts.items())[:SUMMARY_ITEMS]]
                parts.append(t("gui.validatepanel.summary.keywords", items=", ".join(items)))
            if rep.top_paths:
                items = [t("gui.validatepanel.summary.item", name=_display(p), count=format_count(n))
                         for p, n in rep.top_paths[:SUMMARY_ITEMS]]
                parts.append(t("gui.validatepanel.summary.paths", items=", ".join(items)))
        elif st.status == "cancelled":
            parts.append(t("gui.validatepanel.cancelled", issues=format_count(len(st.issues))))
        elif st.status == "failed":
            parts.append(t("gui.validatepanel.failed", error=_error_text(st.error)))
        if st.message:
            parts.append(st.message)
        return "\n".join(parts)

    def _warnings(self, st: _ValidateState | None) -> str:
        if st is None or st.report is None or st.status != "done":
            return ""
        try:
            return "\n".join(st.report.warning_texts())
        except Exception:  # pragma: no cover - texts only
            log.debug("warning texts failed", exc_info=True)
            return ""

    def _on_task_updated(self, task: Any) -> None:
        st = self.state()
        if st is None or st.task is not task or not st.running:
            return
        self._update_progress(task)

    def _update_progress(self, task: Any) -> None:
        info = task.info() if task is not None else None
        if task is not None and getattr(task, "queued", False):
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setToolTip(t("gui.tasks.queued"))
            return
        self.progress_bar.setToolTip(copyfmt.escape_surrogates(info.message) if info is not None else "")
        if info is None or info.fraction is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(max(0.0, min(1.0, info.fraction)) * 1000))

    # ================================================================================
    # validation
    # ================================================================================
    def options(self) -> dict[str, Any]:
        """The validation options of the inputs."""
        return {"schema": self.schema_path(), "scope": self.scope(), "source": self.source_choice(),
                "formats": self.formats(), "max_issues": self.max_issues(), "per_record": self.max_per_record()}

    def _check_ready(self, session: Any) -> str | None:
        """The reason a validation / export of ``session`` cannot start now (``None``: it can)."""
        if session is None or getattr(session, "closed", False) or session.doc is None:
            return t("gui.validatepanel.no_document")
        if not self.schema_path():
            return t("gui.validatepanel.schema.need")
        return None

    def _source_for(self, session: Any, choice: str) -> tuple[bool, str | None]:
        """``(use the view, refusal)`` for the record choice ``choice``."""
        doc = session.doc
        if choice != "view" or doc.kind == "single" or session.view_is_base:
            return False, None
        if getattr(session, "view_busy", False):
            return True, t("gui.validatepanel.view_busy")
        return True, None

    def run_validation(self) -> Any:
        """Validate the current session's document with the inputs (a running validation of the
        same session is stopped). Returns the task, or ``None`` when refused (the reason is shown)."""
        session = self._session
        why = self._check_ready(session)
        if why is not None:
            self._set_error(why)
            return None
        opts = self.options()
        use_view, why = self._source_for(session, opts["source"])
        if why is not None:
            self._set_error(why)
            return None
        self._set_error(None)
        self._set_note(None)
        st = self._state_for(session)
        assert st is not None
        self._stop(st)
        st.release()
        st.gen += 1
        gen = st.gen
        st.status = "running"
        st.opts = opts
        st.issues = []
        st.live_count = 0
        st.error = None
        st.message = ""
        st.outdated = False
        st.started = time.monotonic()
        st.elapsed = 0.0
        # the state shown now (§12.9): the pinned document and the view set of that state
        doc, view_set = session_snapshot(session)
        source = view_set if use_view else None
        st.data_gen = getattr(doc, "gen", None)
        st.view_used = source is not None
        st.source_label = t("gui.validatepanel.summary.view", label=session.view_label()) if source is not None else ""
        schema_path = opts["schema"]
        st.schema_name = os.path.basename(schema_path)
        scope, formats = opts["scope"], opts["formats"]
        max_issues, per_record = opts["max_issues"], opts["per_record"]
        stream = _streaming_validate()

        def work(ctx: Any) -> Any:
            ctx.progress.set_message(t("gui.validatepanel.progress.schema", name=os.path.basename(schema_path)))
            value = jschema.load_schema(schema_path)
            compiled = jschema.compile_schema(value, formats=formats)
            check(ctx.cancel)
            ctx.progress.set_message("")       # validate() names its own step
            kwargs: dict[str, Any] = {"scope": scope, "max_per_record": per_record, "max_issues": max_issues,
                                      "formats": formats, "progress": ctx.progress, "cancel": ctx.cancel}
            if stream:
                def on_issues(batch: Any) -> None:
                    if isinstance(batch, jschema.Issue):     # one issue at a time is accepted too
                        batch = [batch]
                    if batch:
                        ctx.post(list(batch))

                kwargs["on_issues"] = on_issues
            return jschema.validate(doc, compiled, source, **kwargs)

        st.task = self.app.tasks.run(
            work, title=t("gui.validatepanel.task", name=session.name), kind="validate", owner=self._owner,
            heavy=True,
            on_item=lambda item: self._on_item(st, gen, item),
            on_done=lambda report: self._on_done(st, gen, report),
            on_error=lambda exc: self._on_failed(st, gen, exc),
            on_cancel=lambda: self._on_cancelled(st, gen))
        keep_for(session, st.task, sets=(source,), doc=doc)   # the edit state it reads stays readable
        self._remember()
        if st.session is self._session:
            self.grid.reset()
            self._refresh()
        self.validation_started.emit(session)
        return st.task

    def cancel_validation(self) -> None:
        """Stop the current session's validation."""
        st = self.state()
        if st is not None and st.running and st.task is not None:
            st.task.cancel()

    def _stop(self, st: _ValidateState) -> None:
        if st.running and st.task is not None:
            st.task.cancel()
        st.task = None

    def _on_item(self, st: _ValidateState, gen: int, item: Any) -> None:
        if gen != st.gen or not isinstance(item, list):
            return
        st.live_count += len(item)
        limit = int((st.opts or {}).get("max_issues") or DEFAULT_MAX_ISSUES)
        room = limit - len(st.issues)
        if room > 0 and item:
            st.issues.extend(item[:room])
        if st.session is self._session:
            self.grid.rows_changed()
            self._refresh()
        self.issues_changed.emit(st.session)

    def _on_done(self, st: _ValidateState, gen: int, report: Any) -> None:
        if gen != st.gen or self._shut:
            try:
                report.close()
            except Exception:  # pragma: no cover - close never raises
                log.debug("closing a stale report failed", exc_info=True)
            return
        st.task = None
        st.status = "done"
        st.report = report
        st.lease = _Lease(report)
        st.issues = list(report.issues)
        st.live_count = int(report.issue_count)
        st.elapsed = float(report.elapsed)
        self._finished(st, "done")

    def _on_cancelled(self, st: _ValidateState, gen: int) -> None:
        if gen != st.gen:
            return
        st.task = None
        st.status = "cancelled"
        st.elapsed = time.monotonic() - st.started
        self._finished(st, "cancelled")

    def _on_failed(self, st: _ValidateState, gen: int, exc: BaseException) -> None:
        if gen != st.gen:
            return
        st.task = None
        st.status = "failed"
        st.error = exc
        st.elapsed = time.monotonic() - st.started
        self._finished(st, "failed")
        if not isinstance(exc, (JsonReshapeError, Cancelled)):
            self.app.report_error(exc, self)

    def _finished(self, st: _ValidateState, status: str) -> None:
        if st.session is self._session:
            self.grid.rows_changed()
            self._refresh()
        self.issues_changed.emit(st.session)
        self.validation_finished.emit(st.session, status)

    # ================================================================================
    # issues
    # ================================================================================
    def issue_count_shown(self) -> int:
        """Rows of the issue grid."""
        return self._source.row_count()

    def issue_at(self, row: int) -> Any:
        st = self._source.state
        if st is None or not 0 <= row < len(st.issues):
            return None
        return st.issues[row]

    def activate_issue(self, row: int) -> NodeRef | None:
        """Select and reveal the node of issue ``row`` (``None`` when the issue concerns the record
        container: the panel says so)."""
        issue = self.issue_at(row)
        session = self._session
        if issue is None or session is None or getattr(session, "closed", False) or session.doc is None:
            return None
        ref = issue_ref(session, issue)
        if ref is None:
            self._set_note(t("gui.validatepanel.whole_document"))
            return None
        self._set_note(None)
        session.select(ref, reveal=True)
        return ref

    def selected_rows(self) -> list[int]:
        """Selected issue rows (the current row when nothing is selected), ascending."""
        rows: set[int] = set()
        n = self.issue_count_shown()
        for r0, r1, _c0, _c1 in self.grid.selected_ranges():
            rows.update(range(max(0, r0), min(r1, n - 1) + 1))
        if not rows:
            cur = self.grid.current()[0]
            if 0 <= cur < n:
                rows.add(cur)
        return sorted(rows)

    def copy_rows(self, rows: list[int] | None = None) -> str:
        """Copy issues as tab-separated lines (record, key, path, keyword, message)."""
        rows = self.selected_rows() if rows is None else rows
        lines = []
        for row in rows:
            issue = self.issue_at(row)
            if issue is None:
                continue
            lines.append(["" if issue.record is None else str(issue.record), issue.key or "", issue.path_text,
                          issue.keyword, issue.message])
        if not lines:
            return ""
        return set_clipboard_text(copyfmt.tsv_text(lines))

    def issue_menu(self, row: int) -> QMenu:
        """The context menu of issue ``row`` (actions with object names ``show_in_tree``,
        ``copy_message``, ``copy_path``, ``copy_rows``)."""
        menu = QMenu(self)
        issue = self.issue_at(row)
        session = self._session
        can_show = issue is not None and session is not None and issue_ref(session, issue) is not None
        items: list[tuple[str, str, Callable[[], Any], bool]] = [
            ("show_in_tree", t("gui.validatepanel.menu.show"), lambda: self.activate_issue(row), can_show),
            ("copy_message", t("gui.validatepanel.menu.copy_message"),
             lambda: set_clipboard_text(issue.message) if issue is not None else None, issue is not None),
            ("copy_path", t("gui.validatepanel.menu.copy_path"),
             lambda: set_clipboard_text(issue.path_text) if issue is not None else None, issue is not None),
            ("copy_rows", t("gui.validatepanel.menu.copy_rows"), lambda: self.copy_rows(),
             self.issue_count_shown() > 0),
        ]
        for name, text, callback, enabled in items:
            if name == "copy_message":
                menu.addSeparator()
            action = QAction(text, menu)
            action.setObjectName(name)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _checked=False, cb=callback: cb())
            if name == "copy_rows":
                action.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))
            menu.addAction(action)
        return menu

    def _context_menu(self, row: Any, _col: int, pos: QPoint) -> None:
        row = int(row)
        if row < 0:
            return
        if not self.grid.is_selected(row):
            self.grid.set_current(row)
        menu = self.issue_menu(row)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.popup(pos)

    def _grid_key(self, event: QKeyEvent) -> bool:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_rows()
            return True
        return False

    # ================================================================================
    # invalid records as the view
    # ================================================================================
    def _can_show_records(self, st: _ValidateState | None) -> tuple[bool, str]:
        session = self._session
        doc = session.doc if session is not None and not getattr(session, "closed", False) else None
        if doc is None or st is None:
            return False, t("gui.validatepanel.records.need")
        if doc.kind == "single":
            return False, t("gui.validatepanel.records.single")
        if st.status != "done" or st.report is None or st.lease is None:
            return False, t("gui.validatepanel.records.need")
        if st.outdated or getattr(st.report, "gen", None) != getattr(doc, "gen", None):
            return False, t("gui.validatepanel.records.outdated")
        if not st.report.invalid:
            return False, t("gui.validatepanel.records.none")
        return True, ""

    def show_invalid_records(self) -> Any:
        """Show the invalid records as the session's view (``session.show_records``; the set is
        copied in a silent task). Returns the task (``None`` when not possible: the reason is shown)."""
        st = self.state()
        session = self._session
        ok, why = self._can_show_records(st)
        if not ok or st is None or session is None:
            if why:
                self._set_error(why)
            return None
        self._set_error(None)
        lease = st.lease
        assert lease is not None
        report = lease.acquire()
        rs = report.records
        gen = report.gen
        label = t("gui.validatepanel.records_label", name=st.schema_name or "schema")

        def work(ctx: Any) -> RecordSet:
            return _copy_records(rs, gen, label, ctx.cancel)

        def done(copy: RecordSet) -> None:
            lease.done_using()
            if getattr(session, "closed", False):
                copy.close()
                return
            session.show_records(copy, label)
            self.records_opened.emit(session)

        def failed(exc: BaseException) -> None:
            lease.done_using()
            if not self._shut:
                self._set_error(_error_text(exc))
            if not isinstance(exc, JsonReshapeError):
                self.app.report_error(exc, self)

        return self.app.tasks.run(work, title=t("gui.validatepanel.task.records"), kind="records",
                                  owner=self._owner, silent=True, on_done=done, on_error=failed,
                                  on_cancel=lease.done_using)

    # ================================================================================
    # export
    # ================================================================================
    def choose_export_file(self) -> None:
        """Ask for the output file (CSV / JSON Lines), then :meth:`export_issues_to`."""
        session = self._session
        why = self._check_ready(session)
        if why is not None:
            self._set_error(why)
            return
        start = os.path.join(os.path.dirname(session.path), os.path.splitext(session.name)[0] + "-schema-errors.csv")
        path = self._ask_save(self, t("gui.validatepanel.export.title"), start, t("gui.validatepanel.export.filter"))
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".csv"
        self.export_issues_to(path)

    def export_issues_to(self, path: str | os.PathLike[str], fmt: str | None = None) -> Any:
        """Write EVERY issue of the document (validated again with the current inputs on a document
        pinned now) to ``path`` (``fmt``: ``csv`` | ``jsonl``; default from the extension). Returns
        the task (``None`` when refused: the reason is shown)."""
        session = self._session
        why = self._check_ready(session)
        if why is not None:
            self._set_error(why)
            return None
        out = os.path.abspath(os.fspath(path))
        kind = (fmt or EXPORT_FORMATS.get(os.path.splitext(out)[1].lower()) or "").lower()
        opts = self.options()
        use_view, why = self._source_for(session, opts["source"])
        try:
            if kind not in ("csv", "jsonl"):
                raise UserInputError("gui.validatepanel.export.bad_format", name=os.path.basename(out))
            if why is not None:
                raise UserInputError("gui.validatepanel.view_busy")
            live = session.doc
            check_not_source(out, (live.source_path, live.data_path, session.path, opts["schema"]))
        except UserInputError as exc:
            self._set_error(exc.message)
            return None
        self._set_error(None)
        # a file another tab shows: refused while it has changes, else asked; that tab reloads it (review S3)
        reload = open_target_check(self, out, exclude=session, on_refused=self._set_error)
        if reload is None:
            return None
        doc, view_set = session_snapshot(session)
        source = view_set if use_view else None
        schema_path = opts["schema"]
        name = os.path.basename(out)

        def work(ctx: Any) -> int:
            value = jschema.load_schema(schema_path)
            compiled = jschema.compile_schema(value, formats=opts["formats"])
            issues = jschema.iter_issues(doc, compiled, source, scope=opts["scope"],
                                         max_per_record=opts["per_record"], formats=opts["formats"],
                                         progress=ctx.progress, cancel=ctx.cancel)
            try:
                return write_issues(issues, out, kind, cancel=ctx.cancel)
            finally:
                close = getattr(issues, "close", None)
                if callable(close):
                    close()

        def done(count: int) -> None:
            self._export_task = None
            reload_tabs(reload)
            message = t("gui.validatepanel.export.done", count=format_count(count), path=out)
            _status(self.app, message)
            if not self._shut:
                self._refresh_buttons()
            self.export_finished.emit(out, int(count))

        def failed(exc: BaseException) -> None:
            self._export_task = None
            if not self._shut:
                self._set_error(t("gui.validatepanel.failed", error=_error_text(exc)))
                self._refresh_buttons()
            if not isinstance(exc, JsonReshapeError):
                self.app.report_error(exc, self)

        def cancelled() -> None:
            self._export_task = None
            if not self._shut:
                self._refresh_buttons()

        self._export_task = self.app.tasks.run(work, title=t("gui.validatepanel.export.task", name=name),
                                               kind="export", owner=self._owner, heavy=True, on_done=done,
                                               on_error=failed, on_cancel=cancelled)
        keep_for(session, self._export_task, sets=(source,), doc=doc)
        self._refresh_buttons()
        return self._export_task

    # ================================================================================
    # lifetime
    # ================================================================================
    def shutdown(self) -> None:
        """Stop every task of the panel and release the results (the window closes)."""
        if self._shut:
            return
        self._shut = True
        self._schema_timer.stop()
        try:
            self.app.tasks.task_updated.disconnect(self._on_task_updated)
        except (RuntimeError, TypeError):
            pass
        try:
            theme.notifier().theme_changed.disconnect(self._style_labels)
        except (RuntimeError, TypeError):
            pass
        self.app.tasks.detach(self._owner)
        for st in list(self._states.values()):
            st.gen += 1
            st.task = None
            if st.running:
                st.status = "cancelled"
            st.release()
            self._disconnect(st.session)
        self._states.clear()
        self._session = None
        self._source.state = None


class ValidateDialog(QDialog):
    """A non-modal window with a :class:`ValidatePanel` for one session (closed with its session)."""

    _OPEN: weakref.WeakKeyDictionary[Any, ValidateDialog] = weakref.WeakKeyDictionary()

    def __init__(self, session: Any, parent: QWidget | None = None, *, schema_path: str | None = None,
                 ask_open_path: PathAsker | None = None, ask_save_path: PathAsker | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setObjectName("validate_dialog")
        self.setWindowTitle(t("gui.validatepanel.title", name=session.name))
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.panel = ValidatePanel(session.app, self, ask_open_path=ask_open_path, ask_save_path=ask_save_path)
        layout.addWidget(self.panel, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        close = QPushButton(t("gui.common.close"), self)
        close.setAutoDefault(False)
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)
        self.panel.set_session(session)
        if schema_path:
            self.panel.set_schema_path(schema_path)
        session.state_changed.connect(self._on_session_state)
        self.resize(940, 620)

    def _on_session_state(self, value: str) -> None:
        if value == "closed":
            self.close()

    def done(self, result: int) -> None:  # noqa: D401 - Qt name
        self.panel.shutdown()
        super().done(result)

    @classmethod
    def show_for(cls, session: Any, parent: QWidget | None = None, **kwargs: Any) -> ValidateDialog:
        """The validation window of ``session`` (an open one is brought to the front; a given
        ``schema_path`` is used), shown non-modal."""
        dialog = cls._OPEN.get(session)
        if dialog is None or not _alive(dialog) or dialog.panel._shut:
            dialog = cls(session, parent, **kwargs)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            if parent is None:
                keep_alive(dialog)
            cls._OPEN[session] = dialog
        elif kwargs.get("schema_path"):
            dialog.panel.set_schema_path(kwargs["schema_path"])
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog


def show_validate_dialog(session: Any, parent: QWidget | None = None, **kwargs: Any) -> ValidateDialog:
    """Tools ▸ "Schema bo'yicha tekshirish…": :meth:`ValidateDialog.show_for`."""
    return ValidateDialog.show_for(session, parent, **kwargs)
