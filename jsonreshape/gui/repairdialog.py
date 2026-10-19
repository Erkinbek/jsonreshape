"""Repair dialog (docs/ARCHITECTURE.md §14.5, §14.9; design: the stage-5 analysis ``s5/map_repair_diff.md``
§3.8; core: :mod:`jsonreshape.repair`; AGENT_PROMPT §10 "Ta'mirlash").

:class:`RepairDialog` (non-modal) makes a strictly valid copy of a broken JSON / JSON Lines file with
:func:`jsonreshape.repair.repair_file` and always shows the report before anything is opened:

* source: the tab's file (the ``source`` argument) or any file ("Tanlash…", the injected
  ``ask_open_path``); target: default ``<stem>.repaired<ext>`` beside the source (a ``.gz`` name stays
  gzip), "Tanlash…" with the injected ``ask_save_path``. The target is NEVER the source: it is refused when
  it names the source file, also under another spelling or through a link
  (:func:`~jsonreshape.gui.searchpanel.check_not_source`: normalized path and ``os.path.samefile``) -- and
  :func:`repair_file` checks it once more (``writer.error.same_file``). An existing target is overwritten
  only after a confirmation. A target -- or a saved report -- that another tab shows is refused while that
  tab has unsaved, pending or running changes or another window of the program edits the file, and asked
  about (instead of the overwrite question) when the tab shows it unchanged; that tab opens the new file
  once it was written (review S3, :func:`~jsonreshape.gui.searchpanel.open_target_check`);
* options: the input format (detected, or forced: a broken head may be detected wrongly, decision D10)
  and the record path, NaN / Infinity -> ``null`` or text, JSON Lines output for concatenated values,
  "keep the records before an unrepairable place" (``allow_partial``); ``hints`` (``format``,
  ``record_path``: what the tab was opened with) preset the first two;
* "Tekshirish" (``check_only``: nothing is written) and "Ta'mirlash" run as ONE heavy task (kind
  ``repair``) with progress (bytes, rate, time left; "Navbatda" while it waits for a heavy-task slot) and
  "To'xtatish" (cancel: nothing is written);
* the report: the headline (``report.headline()``; "no problems" when there was nothing to fix), the
  input and output kinds, the fixes that changed or dropped data (``data_changes_line``), a summary grid by
  fix code (category, localized name, count, the first location -- activating a row calls the
  ``show_location`` callback with its :class:`RepairLocation`), the truncation box (where the data ends,
  records kept, bytes dropped, closers appended; "Joyini ko'rsatish"), the unrepairable box (the reason,
  two lines of context with a caret, "Joyini ko'rsatish", "Qisman natijani saqlash" = the same repair
  again with ``allow_partial=True``), a :class:`~jsonreshape.gui.vgrid.VirtualGrid` of the listed fixes
  (``report.fixes``: the first 10 000; the rest via "Hisobotni saqlash…") and the warnings;
* "Hisobotni saqlash…" writes every fix (``report.write``: CSV / JSON / JSON Lines by the extension) in a
  task; "Ta'mirlangan faylni ochish" calls ``open_repaired(target, report.reopen_options())`` (default:
  ``app.request_open``, a new tab).

Report lifetime (``RepairReport.close()`` removes its temporary fix list): the dialog owns the report it
shows and closes it when the next run starts, when the dialog closes (``done``) and when it is destroyed.
A save of the report leases it, so it is closed only after the file is written. The task of a run belongs
to a small job object (not to the dialog): a report that arrives after the dialog closed -- or for a run
the dialog no longer waits for -- is closed at once; an ``UnrepairableError`` is turned into a return value
in the worker, so its report can never be dropped by a cancellation. Closing the dialog while a run is
going asks first and stops it (a repair whose report nobody sees would break "always a report").

``show_location`` receives a :class:`RepairLocation` of the SOURCE file: ``offset`` is the byte offset of
the user's file exactly like the reader's JSON errors (for an uncompressed UTF-8 file the raw view's data
offset; for gzip / UTF-16 sources show ``line`` instead), ``line`` / ``column`` are 1-based (column in
characters). Without the callback the location buttons say the file is not open.

Integration API (round B): ``RepairDialog(app, source=None, parent=None, *, hints=None, target=None,
show_location=None, open_repaired=None, ask_open_path=None, ask_save_path=None)`` and
``show_repair_dialog(app, source=None, parent=None, **kw)`` (one window per source path, brought to the
front when open). The path askers are ``fn(parent, title, start_path, filters) -> str | None``.
"""

from __future__ import annotations

import logging
import os
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import shiboken6
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jsonreshape.errors import JsonReshapeError, UserInputError
from jsonreshape.fileio import long_path, normalize_path
from jsonreshape.gui import copyfmt, theme
from jsonreshape.gui.searchpanel import check_not_source, open_target_check, reload_tabs
from jsonreshape.gui.vgrid import Cell, Column, VirtualGrid
from jsonreshape.gui.widgets import confirm, format_count, keep_alive, mono_font, set_clipboard_text
from jsonreshape.i18n import has, t
from jsonreshape.paths import format_path, parse_path
from jsonreshape.reader import KINDS
from jsonreshape.repair import FIX_CATEGORY, FIX_CODES, RepairOptions, UnrepairableError, repair_file
from jsonreshape.tasks import format_bytes, format_duration

log = logging.getLogger(__name__)

__all__ = [
    "NAN_POLICIES",
    "REPORT_FORMATS",
    "RepairDialog",
    "RepairLocation",
    "default_report_path",
    "default_target",
    "location_of",
    "show_repair_dialog",
]

REPORT_FORMATS = {".csv": "csv", ".json": "json", ".jsonl": "jsonl"}
NAN_POLICIES = ("null", "string")
SUMMARY_ROWS_VISIBLE = 6        # summary grid height (rows; it has at most one row per fix code)

PathAsker = Callable[[Any, str, str, str], "str | None"]   # (parent, title, start path, filters) -> path

_CATEGORY_ROLE = {"syntax": "muted", "data": "warn", "dropped": "danger"}
_DISPLAY_MAP = {ord("\n"): "↵", ord("\r"): "↵", ord("\t"): "→"}
_DISPLAY_MAP.update({c: "·" for c in range(0x20) if c not in (9, 10, 13)})

_OPEN: dict[str, weakref.ReferenceType[Any]] = {}   # normalized source path -> its dialog (show_repair_dialog)


# ==================================================================================
# helpers
# ==================================================================================
def ask_save_file(parent: Any, title: str, start: str, filters: str) -> str | None:
    path, _selected = QFileDialog.getSaveFileName(parent, title, start, filters)
    return path or None


def ask_open_file(parent: Any, title: str, start: str, filters: str) -> str | None:
    path, _selected = QFileDialog.getOpenFileName(parent, title, start, filters)
    return path or None


def _alive(obj: Any) -> bool:
    try:
        return obj is not None and bool(shiboken6.isValid(obj))
    except Exception:  # pragma: no cover - shiboken always available with PySide6
        return False


class _TaskOwner:
    """A plain owner object of a task (never detached: its callbacks must run)."""

    __slots__ = ("__weakref__",)


def _display(text: Any) -> str:
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


def _clean_path(text: str) -> str:
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':     # pasted "C:\...\x.json"
        text = text[1:-1].strip()
    return text


def _kind_name(kind: str | None) -> str:
    if kind and has(f"reader.kind.{kind}"):
        return t(f"reader.kind.{kind}")
    return str(kind or "")


def _close_report(report: Any) -> None:
    try:
        report.close()
    except Exception:  # pragma: no cover - close never raises
        log.debug("closing a repair report failed", exc_info=True)


def default_target(source: str | os.PathLike[str]) -> str:
    """``<stem>.repaired<ext>`` beside ``source`` (``data.json.gz`` -> ``data.repaired.json.gz``: the
    output stays gzip; no extension -> ``.json``)."""
    p = Path(os.fspath(source))
    name = p.name
    gz = name.lower().endswith(".gz")
    base = name[:-3] if gz else name
    stem, ext = os.path.splitext(base)
    if not stem:
        stem, ext = base, ""
    if not ext:
        ext = ".json"
    return str(p.with_name(f"{stem}.repaired{ext}{'.gz' if gz else ''}"))


def default_report_path(source: str | os.PathLike[str]) -> str:
    """``<stem>.repair-report.csv`` beside ``source``."""
    p = Path(os.fspath(source))
    name = p.name[:-3] if p.name.lower().endswith(".gz") else p.name
    stem = os.path.splitext(name)[0] or name
    return str(p.with_name(f"{stem}.repair-report.csv"))


@dataclass(frozen=True)
class RepairLocation:
    """A place in the SOURCE file (a fix, the cut of a truncated file, the unrepairable problem):
    ``path`` (the source), ``offset`` (byte offset of the user's file, like the reader's JSON errors),
    ``line`` / ``column`` (1-based; the column in characters), ``length`` (bytes, at least 1) and
    ``what`` (the fix code, ``truncated`` or the problem code)."""

    path: str
    offset: int
    line: int
    column: int
    length: int = 1
    what: str = ""


def location_of(source: str | os.PathLike[str], item: Any, what: str | None = None) -> RepairLocation:
    """The :class:`RepairLocation` of a ``RepairFix``, ``RepairProblem`` or ``Truncation``."""
    length = int(getattr(item, "length", 0) or 0)
    code = what if what is not None else str(getattr(item, "code", "") or "")
    return RepairLocation(path=os.fspath(source), offset=int(item.offset), line=int(item.line),
                          column=int(item.column), length=max(1, length), what=code)


class _Lease:
    """A report shared by the dialog and background tasks: closed once the dialog released it and no
    task uses it any more (GUI thread only)."""

    def __init__(self, report: Any) -> None:
        self.report = report
        self.users = 0
        self.released = False
        self.closed = False

    def acquire(self) -> Any:
        self.users += 1
        return self.report

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
        if not self.closed:
            self.closed = True
            _close_report(self.report)


class _Holder:
    """The dialog's current lease, reachable without the dialog (released when it is destroyed)."""

    __slots__ = ("lease",)

    def __init__(self) -> None:
        self.lease: _Lease | None = None

    def release(self, *_args: Any) -> None:
        lease, self.lease = self.lease, None
        if lease is not None:
            lease.release()


class _RepairJob:
    """One check / repair run: the task's owner and callbacks (plain Python). A result the dialog does
    not take any more (closed, superseded, destroyed) has its report closed at once."""

    def __init__(self, dialog: RepairDialog, *, check_only: bool, partial: bool, reload: list | None = None) -> None:
        self._dialog = weakref.ref(dialog)
        self.check_only = check_only
        self.partial = partial
        self.reload = list(reload or ())     # weak refs: tabs that showed the target (reopened once it is written)
        self.task: Any = None
        self.state = "running"

    def dialog(self) -> RepairDialog | None:
        d = self._dialog()
        if d is None or not _alive(d) or not d._takes(self):
            return None
        return d

    def done(self, result: Any) -> None:
        if isinstance(result, BaseException):          # an UnrepairableError handed over by the worker
            self.failed(result)
            return
        self.state = "done"
        reload, self.reload = self.reload, []
        if getattr(result, "written", False):
            reload_tabs(reload)                       # also when nobody waits for the report any more
        d = self.dialog()
        if d is None:
            _close_report(result)
            return
        d._job_done(self, result)

    def failed(self, exc: BaseException) -> None:
        self.state = "failed"
        report = getattr(exc, "report", None)
        d = self.dialog()
        if d is None:
            if report is not None:
                _close_report(report)
            return
        d._job_failed(self, exc)

    def cancelled(self) -> None:
        self.state = "cancelled"
        d = self.dialog()
        if d is not None:
            d._job_cancelled(self)


class _SummarySource:
    """GridSource of the summary: one row per fix code ``(code, count, first fix)``."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, int, Any]] = []

    def row_count(self) -> int:
        return len(self.rows)

    def cell(self, row: int, column: Column) -> Cell | None:
        if not 0 <= row < len(self.rows):
            return None
        code, count, first = self.rows[row]
        category = FIX_CATEGORY.get(code, "syntax")
        cid = column.id
        if cid == "category":
            return Cell(t(f"repair.category.{category}"), role=_CATEGORY_ROLE.get(category))
        if cid == "fix":
            name = first.name if first is not None else t(f"repair.fix.{code}")
            return Cell(_display(name))
        if cid == "count":
            return Cell(format_count(count), align="right")
        if first is None:
            return Cell("")
        return Cell(t("gui.repairdialog.location", line=first.line, column=first.column), role="link",
                    tooltip=t("gui.repairdialog.location.tip", offset=first.offset))

    def rows_shown(self, first: int, last: int) -> None:
        return None

    def row_label(self, row: int) -> str | None:
        return None


class _FixSource:
    """GridSource of the listed fixes (``report.fixes``)."""

    def __init__(self) -> None:
        self.fixes: list[Any] = []

    def row_count(self) -> int:
        return len(self.fixes)

    def cell(self, row: int, column: Column) -> Cell | None:
        if not 0 <= row < len(self.fixes):
            return None
        fx = self.fixes[row]
        cid = column.id
        if cid == "fix":
            tip = fx.category_name
            if fx.reason:
                reason = t(f"repair.problem.{fx.reason}") if has(f"repair.problem.{fx.reason}") \
                    else t(f"repair.fix.{fx.reason}") if has(f"repair.fix.{fx.reason}") else fx.reason
                tip += "\n" + reason
            role = _CATEGORY_ROLE.get(fx.category) if fx.category != "syntax" else None
            return Cell(_display(fx.name), role=role, tooltip=tip)
        if cid == "line":
            return Cell(str(fx.line), align="right")
        if cid == "column":
            return Cell(str(fx.column), align="right")
        if cid in ("before", "after"):
            text = fx.before if cid == "before" else fx.after
            if not text:
                return Cell(t("gui.repairdialog.nothing"), role="muted", italic=True)
            return Cell(_display(text), role="string")
        if cid == "record":
            return Cell("" if fx.record is None else f"#{fx.record}", role="muted",
                        tooltip=t("gui.repairdialog.col.record.tip"))
        return None

    def rows_shown(self, first: int, last: int) -> None:
        return None

    def row_label(self, row: int) -> str | None:
        return None


# ==================================================================================
# the dialog
# ==================================================================================
class RepairDialog(QDialog):
    """Repair a broken JSON file into a new file and show the report (module docstring).

    Signals: ``run_started(task)``, ``run_finished(report)`` (the dialog owns and closes the report),
    ``run_failed(exc)`` (an ``UnrepairableError``'s report is shown too), ``run_cancelled()``,
    ``report_saved(path)``."""

    run_started = Signal(object)
    run_finished = Signal(object)
    run_failed = Signal(object)
    run_cancelled = Signal()
    report_saved = Signal(str)

    def __init__(self, app: Any, source: str | os.PathLike[str] | None = None, parent: QWidget | None = None, *,
                 hints: dict[str, Any] | None = None, target: str | os.PathLike[str] | None = None,
                 show_location: Callable[[RepairLocation], Any] | None = None,
                 open_repaired: Callable[[str, dict], Any] | None = None,
                 ask_open_path: PathAsker | None = None, ask_save_path: PathAsker | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self._show_location_cb = show_location
        self._open_cb = open_repaired
        self._ask_open = ask_open_path or ask_open_file
        self._ask_save = ask_save_path or ask_save_file
        self._job: _RepairJob | None = None
        self._lease: _Lease | None = None
        self._holder = _Holder()
        self._closing = False
        self._target_auto = True
        self._queued_shown = False
        self.setObjectName("repair_dialog")
        self.setSizeGripEnabled(True)
        self._build()
        self._apply_hints(hints or {})
        if source:
            self.set_source_path(source)
        if target:
            self.set_target_path(target)
        self._update_title()
        app.tasks.task_updated.connect(self._on_task_updated)
        self.destroyed.connect(self._holder.release)
        theme.notifier().theme_changed.connect(self._style_labels)
        self._refresh()
        self.resize(900, 700)

    # ------------------------------------------------------------------ construction
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        intro = QLabel(t("gui.repairdialog.intro"), self)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        files = QFormLayout()
        self.source_edit = QLineEdit(self)
        self.source_edit.setObjectName("source_edit")
        self.source_edit.setClearButtonEnabled(True)
        self.source_edit.textChanged.connect(self._on_source_changed)
        self.source_button = QPushButton(t("gui.repairdialog.browse"), self)
        self.source_button.setAutoDefault(False)
        self.source_button.clicked.connect(self.choose_source)
        row = QHBoxLayout()
        row.addWidget(self.source_edit, 1)
        row.addWidget(self.source_button)
        source_label = QLabel(t("gui.repairdialog.source.label"), self)
        source_label.setBuddy(self.source_edit)
        files.addRow(source_label, row)
        self.target_edit = QLineEdit(self)
        self.target_edit.setObjectName("target_edit")
        self.target_edit.setClearButtonEnabled(True)
        self.target_edit.textEdited.connect(self._on_target_edited)
        self.target_edit.textChanged.connect(lambda _text: self._refresh())
        self.target_button = QPushButton(t("gui.repairdialog.browse"), self)
        self.target_button.setAutoDefault(False)
        self.target_button.clicked.connect(self.choose_target)
        row = QHBoxLayout()
        row.addWidget(self.target_edit, 1)
        row.addWidget(self.target_button)
        target_label = QLabel(t("gui.repairdialog.target.label"), self)
        target_label.setBuddy(self.target_edit)
        files.addRow(target_label, row)
        layout.addLayout(files)

        self.options_box = QGroupBox(t("gui.repairdialog.options"), self)
        form = QFormLayout(self.options_box)
        self.format_combo = QComboBox(self.options_box)
        self.format_combo.addItem(t("gui.repairdialog.format.auto"), "")
        for kind in KINDS:
            self.format_combo.addItem(_kind_name(kind), kind)
        self.format_combo.setToolTip(t("gui.repairdialog.format.tip"))
        self.record_path_edit = QLineEdit(self.options_box)
        self.record_path_edit.setPlaceholderText(t("gui.repairdialog.record_path.placeholder"))
        self.record_path_edit.setClearButtonEnabled(True)
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(self.format_combo)
        rp_label = QLabel(t("gui.repairdialog.record_path.label"), self.options_box)
        rp_label.setBuddy(self.record_path_edit)
        fmt_row.addWidget(rp_label)
        fmt_row.addWidget(self.record_path_edit, 1)
        fmt_label = QLabel(t("gui.repairdialog.format.label"), self.options_box)
        fmt_label.setBuddy(self.format_combo)
        form.addRow(fmt_label, fmt_row)
        self.nan_combo = QComboBox(self.options_box)
        for policy in NAN_POLICIES:
            self.nan_combo.addItem(t(f"gui.repairdialog.nan.{policy}"), policy)
        nan_label = QLabel(t("gui.repairdialog.nan.label"), self.options_box)
        nan_label.setBuddy(self.nan_combo)
        form.addRow(nan_label, self.nan_combo)
        self.jsonl_check = QCheckBox(t("gui.repairdialog.jsonl"), self.options_box)
        self.jsonl_check.setToolTip(t("gui.repairdialog.jsonl.tip"))
        form.addRow(self.jsonl_check)
        self.partial_check = QCheckBox(t("gui.repairdialog.partial"), self.options_box)
        self.partial_check.setToolTip(t("gui.repairdialog.partial.tip"))
        form.addRow(self.partial_check)
        layout.addWidget(self.options_box)

        run = QHBoxLayout()
        # Enter = "Tekshirish": the default action never writes anything
        self.check_button = QPushButton(t("gui.repairdialog.check"), self)
        self.check_button.setToolTip(t("gui.repairdialog.check.tip"))
        self.check_button.setDefault(True)
        self.check_button.clicked.connect(lambda: self.check())
        run.addWidget(self.check_button)
        self.repair_button = QPushButton(t("gui.repairdialog.repair"), self)
        self.repair_button.setToolTip(t("gui.repairdialog.repair.tip"))
        self.repair_button.setAutoDefault(False)
        self.repair_button.clicked.connect(lambda: self.repair())
        run.addWidget(self.repair_button)
        self.stop_button = QPushButton(t("gui.repairdialog.stop"), self)
        self.stop_button.setAutoDefault(False)
        self.stop_button.clicked.connect(self.cancel_run)
        self.stop_button.setVisible(False)
        run.addWidget(self.stop_button)
        run.addStretch(1)
        layout.addLayout(run)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(8)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        self.status_label = self._label("status_label", selectable=True)
        layout.addWidget(self.status_label)
        self.error_label = self._label("error_label")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)
        self.note_label = self._label("note_label")
        self.note_label.setVisible(False)
        layout.addWidget(self.note_label)

        # -- the report ---------------------------------------------------------------------
        self.report_box = QWidget(self)
        self.report_box.setObjectName("report_box")
        rl = QVBoxLayout(self.report_box)
        rl.setContentsMargins(0, 0, 0, 0)
        self.headline_label = self._label("headline_label", selectable=True)
        font = self.headline_label.font()
        font.setBold(True)
        self.headline_label.setFont(font)
        rl.addWidget(self.headline_label)
        self.kind_label = self._label("kind_label")
        rl.addWidget(self.kind_label)
        self.data_label = self._label("data_label", selectable=True)
        rl.addWidget(self.data_label)

        self.summary_grid = VirtualGrid(self.report_box)
        self.summary_grid.setObjectName("summary_grid")
        self.summary_grid.set_selection_mode("single")
        self.summary_grid.set_columns([
            Column("category", t("gui.repairdialog.col.category"), width=200),
            Column("fix", t("gui.repairdialog.col.fix"), width=330),
            Column("count", t("gui.repairdialog.col.count"), width=90, align="right"),
            Column("first", t("gui.repairdialog.col.first"), width=170),
        ])
        self._summary_source = _SummarySource()
        self.summary_grid.set_source(self._summary_source)
        self.summary_grid.set_placeholder_text(t("gui.repairdialog.summary.empty"))
        self.summary_grid.activated.connect(lambda row, _col: self.activate_summary(int(row)))
        self.summary_grid.setMaximumHeight(self.summary_grid.row_height() * (SUMMARY_ROWS_VISIBLE + 2) + 6)
        rl.addWidget(self.summary_grid)

        self.truncation_box = QFrame(self.report_box)
        self.truncation_box.setObjectName("truncation_box")
        self.truncation_box.setFrameShape(QFrame.Shape.StyledPanel)
        tl = QVBoxLayout(self.truncation_box)
        self.truncation_title = self._label("truncation_title", bold=True)
        self.truncation_title.setText(t("gui.repairdialog.truncated.title"))
        tl.addWidget(self.truncation_title)
        self.truncation_label = self._label("truncation_label", selectable=True)
        tl.addWidget(self.truncation_label)
        trow = QHBoxLayout()
        self.truncation_button = QPushButton(t("gui.repairdialog.show_location"), self.truncation_box)
        self.truncation_button.setAutoDefault(False)
        self.truncation_button.clicked.connect(self.show_truncation)
        trow.addWidget(self.truncation_button)
        trow.addStretch(1)
        tl.addLayout(trow)
        rl.addWidget(self.truncation_box)

        self.problem_box = QFrame(self.report_box)
        self.problem_box.setObjectName("problem_box")
        self.problem_box.setFrameShape(QFrame.Shape.StyledPanel)
        pl = QVBoxLayout(self.problem_box)
        self.problem_title = self._label("problem_title", bold=True)
        self.problem_title.setText(t("gui.repairdialog.problem.title"))
        pl.addWidget(self.problem_title)
        self.problem_label = self._label("problem_label", selectable=True)
        pl.addWidget(self.problem_label)
        self.context_label = self._label("context_label", selectable=True)
        self.context_label.setWordWrap(False)
        self.context_label.setFont(mono_font(None))
        pl.addWidget(self.context_label)
        self.partial_label = self._label("partial_label", selectable=True)
        pl.addWidget(self.partial_label)
        prow = QHBoxLayout()
        self.problem_button = QPushButton(t("gui.repairdialog.show_location"), self.problem_box)
        self.problem_button.setAutoDefault(False)
        self.problem_button.clicked.connect(self.show_problem)
        prow.addWidget(self.problem_button)
        self.save_partial_button = QPushButton(t("gui.repairdialog.save_partial"), self.problem_box)
        self.save_partial_button.setToolTip(t("gui.repairdialog.save_partial.tip"))
        self.save_partial_button.setAutoDefault(False)
        self.save_partial_button.clicked.connect(lambda: self.save_partial())
        prow.addWidget(self.save_partial_button)
        prow.addStretch(1)
        pl.addLayout(prow)
        rl.addWidget(self.problem_box)

        self.fixes_label = self._label("fixes_label")
        rl.addWidget(self.fixes_label)
        self.fixes_grid = VirtualGrid(self.report_box)
        self.fixes_grid.setObjectName("fixes_grid")
        self.fixes_grid.set_selection_mode("rows")
        self.fixes_grid.set_columns([
            Column("fix", t("gui.repairdialog.col.fix"), width=300),
            Column("line", t("gui.repairdialog.col.line"), width=80, align="right"),
            Column("column", t("gui.repairdialog.col.column"), width=70, align="right"),
            Column("before", t("gui.repairdialog.col.before"), width=150),
            Column("after", t("gui.repairdialog.col.after"), width=150),
            Column("record", t("gui.repairdialog.col.record"), width=90),
        ])
        self._fix_source = _FixSource()
        self.fixes_grid.set_source(self._fix_source)
        self.fixes_grid.set_placeholder_text(t("gui.repairdialog.fixes.empty"))
        self.fixes_grid.activated.connect(lambda row, _col: self.activate_fix(int(row)))
        self.fixes_grid.set_key_handler(self._fixes_key)
        rl.addWidget(self.fixes_grid, 1)
        self.warnings_label = self._label("warnings_label", selectable=True)
        rl.addWidget(self.warnings_label)
        layout.addWidget(self.report_box, 1)
        self.report_box.setVisible(False)

        bottom = QHBoxLayout()
        self.save_report_button = QPushButton(t("gui.repairdialog.save_report"), self)
        self.save_report_button.setAutoDefault(False)
        self.save_report_button.clicked.connect(lambda: self.save_report())
        bottom.addWidget(self.save_report_button)
        self.open_button = QPushButton(t("gui.repairdialog.open"), self)
        self.open_button.setToolTip(t("gui.repairdialog.open.tip"))
        self.open_button.setAutoDefault(False)
        self.open_button.clicked.connect(self.open_repaired)
        bottom.addWidget(self.open_button)
        bottom.addStretch(1)
        self.close_button = QPushButton(t("gui.common.close"), self)
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self.reject)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)
        self._style_labels()

    def _label(self, name: str, *, selectable: bool = False, bold: bool = False) -> QLabel:
        label = QLabel(self)
        label.setObjectName(name)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        if selectable:
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if bold:
            font = label.font()
            font.setBold(True)
            label.setFont(font)
        return label

    def _style_labels(self) -> None:
        if not _alive(self):
            return
        warn = theme.color("warn").name()
        danger = theme.color("danger").name()
        muted = theme.color("muted").name()
        self.error_label.setStyleSheet(f"color: {danger};")
        self.data_label.setStyleSheet(f"color: {warn};")
        self.warnings_label.setStyleSheet(f"color: {warn};")
        self.note_label.setStyleSheet(f"color: {muted};")
        self.kind_label.setStyleSheet(f"color: {muted};")
        self.truncation_title.setStyleSheet(f"color: {warn};")
        self.problem_title.setStyleSheet(f"color: {danger};")

    def _apply_hints(self, hints: dict[str, Any]) -> None:
        fmt = hints.get("format")
        if isinstance(fmt, str) and fmt in KINDS:
            self.format_combo.setCurrentIndex(self.format_combo.findData(fmt))
        rp = hints.get("record_path")
        if isinstance(rp, str):
            self.record_path_edit.setText(rp)
        elif isinstance(rp, (tuple, list)) and rp:
            try:
                self.record_path_edit.setText(format_path(tuple(rp)))
            except (JsonReshapeError, TypeError, ValueError):
                log.debug("record path hint %r not shown", rp, exc_info=True)

    def _update_title(self) -> None:
        src = self.source_path()
        if src:
            self.setWindowTitle(t("gui.repairdialog.title", name=os.path.basename(src)))
        else:
            self.setWindowTitle(t("gui.repairdialog.title_none"))

    # ------------------------------------------------------------------ inputs
    def source_path(self) -> str:
        return _clean_path(self.source_edit.text())

    def set_source_path(self, path: str | os.PathLike[str]) -> None:
        """The file to repair (the target follows it until the user chose a target)."""
        self.source_edit.setText(os.fspath(path))

    def target_path(self) -> str:
        return _clean_path(self.target_edit.text())

    def set_target_path(self, path: str | os.PathLike[str]) -> None:
        self._target_auto = False
        self.target_edit.setText(os.fspath(path))

    def _on_source_changed(self, _text: str) -> None:
        if self._target_auto:
            src = self.source_path()
            self.target_edit.setText(default_target(src) if src else "")
        self._update_title()
        self._refresh()

    def _on_target_edited(self, _text: str) -> None:
        self._target_auto = False

    def choose_source(self) -> None:
        """"Tanlash…" next to the source."""
        path = self._ask_open(self, t("gui.repairdialog.source.title"), self.source_path(),
                              t("gui.repairdialog.source.filter"))
        if path:
            self.set_source_path(path)

    def choose_target(self) -> None:
        """"Tanlash…" next to the target."""
        src = self.source_path()
        start = self.target_path() or (default_target(src) if src else "")
        path = self._ask_save(self, t("gui.repairdialog.target.title"), start, t("gui.repairdialog.target.filter"))
        if path:
            self.set_target_path(path)

    def options(self, *, allow_partial: bool | None = None) -> RepairOptions:
        """The :class:`~jsonreshape.repair.RepairOptions` of the inputs (``UserInputError`` for an invalid
        record path)."""
        fmt = self.format_combo.currentData() or None
        text = self.record_path_edit.text().strip()
        record_path = None
        if text:
            try:
                record_path = tuple(parse_path(text)) or None
            except JsonReshapeError as exc:
                raise UserInputError("gui.repairdialog.record_path.invalid", error=exc.message) from None
        return RepairOptions(nan=str(self.nan_combo.currentData() or "null"), format=fmt, record_path=record_path,
                             output_kind="jsonl" if self.jsonl_check.isChecked() else None,
                             allow_partial=self.partial_check.isChecked() if allow_partial is None
                             else bool(allow_partial))

    # ------------------------------------------------------------------ messages
    def _set_error(self, text: str | None) -> None:
        self.error_label.setText(copyfmt.escape_surrogates(text or ""))
        self.error_label.setVisible(bool(text))

    def _set_note(self, text: str | None) -> None:
        self.note_label.setText(copyfmt.escape_surrogates(text or ""))
        self.note_label.setVisible(bool(text))

    def error_text(self) -> str:
        return self.error_label.text() if not self.error_label.isHidden() else ""

    def note_text(self) -> str:
        return self.note_label.text() if not self.note_label.isHidden() else ""

    def status_text(self) -> str:
        return self.status_label.text()

    # ------------------------------------------------------------------ running
    @property
    def running(self) -> bool:
        job = self._job
        return job is not None and job.task is not None and job.task.state == "running"

    @property
    def task(self) -> Any:
        """The task of the running (or last started) run."""
        return self._job.task if self._job is not None else None

    @property
    def report(self) -> Any:
        """The :class:`~jsonreshape.repair.RepairReport` shown (owned by the dialog), or ``None``."""
        return self._lease.report if self._lease is not None else None

    def _takes(self, job: _RepairJob) -> bool:
        return job is self._job and not self._closing

    def check(self) -> Any:
        """"Tekshirish": run the repair without writing anything. Returns the task (``None`` when
        refused: the reason is shown)."""
        return self._start(check_only=True)

    def repair(self) -> Any:
        """"Ta'mirlash": write the repaired copy to the target. Returns the task (``None`` when refused)."""
        return self._start(check_only=False)

    def save_partial(self) -> Any:
        """"Qisman natijani saqlash": repair again with ``allow_partial=True`` (the complete records before
        the unrepairable place are written, closed like a truncation)."""
        rep = self.report
        if rep is None or rep.problem is None or rep.written or rep.truncated is None:
            return None
        return self._start(check_only=False, allow_partial=True)

    def _start(self, *, check_only: bool, allow_partial: bool | None = None) -> Any:
        if self.running or self._closing:
            return None
        self._set_note(None)
        src_text = self.source_path()
        if not src_text:
            self._set_error(t("gui.repairdialog.source.need"))
            return None
        src = os.path.abspath(src_text)
        tgt_text = self.target_path()
        tgt = os.path.abspath(tgt_text) if tgt_text else None
        try:
            opts = self.options(allow_partial=allow_partial)
            if not check_only:
                if tgt is None:
                    raise UserInputError("repair.error.no_target")
                # also another name of the source (case, 8.3 name, junction, hard link): checked here and
                # once more by repair_file
                check_not_source(tgt, (src,))
                if os.path.isdir(long_path(tgt)):
                    raise UserInputError("fileio.error.is_directory", path=tgt)
        except JsonReshapeError as exc:
            text = exc.message
            if getattr(exc, "key", "") == "writer.error.same_file":
                text += " " + t("gui.repairdialog.same_file.hint")
            self._set_error(text)
            return None
        reload: list = []
        if not check_only and tgt is not None:
            # a target another tab shows: refused while it has changes, else asked -- instead of the
            # overwrite question -- and that tab reloads the repaired file (review S3)
            self._set_error(None)
            found = open_target_check(self, tgt, on_refused=self._set_error)
            if found is None:
                return None
            reload = found
        if not check_only and tgt is not None and not reload and os.path.exists(long_path(tgt)) \
                and not confirm(self, t("gui.repairdialog.overwrite", path=tgt)):
            return None
        self._set_error(None)
        self._release_report()                 # the report of the previous run is closed now
        settings = getattr(self.app, "settings", None)
        target = None if check_only else tgt
        job = _RepairJob(self, check_only=check_only, partial=bool(opts.allow_partial), reload=reload)

        def work(ctx: Any) -> Any:
            try:
                return repair_file(src, target, opts, check_only=check_only, settings=settings,
                                   progress=ctx.progress, cancel=ctx.cancel)
            except UnrepairableError as exc:
                return exc      # a value, not an error: a cancellation can never drop its report

        name = os.path.basename(src)
        title = t("gui.repairdialog.task.check" if check_only else "gui.repairdialog.task.repair", name=name)
        job.task = self.app.tasks.run(work, title=title, kind="repair", owner=job, heavy=True, on_done=job.done,
                                      on_error=job.failed, on_cancel=job.cancelled)
        self._job = job
        self._queued_shown = False
        self.status_label.setText(title)
        self._refresh()
        self.run_started.emit(job.task)
        return job.task

    def cancel_run(self) -> None:
        """Stop the running check / repair (nothing is written)."""
        if self.running:
            self._job.task.cancel()     # type: ignore[union-attr]

    # ------------------------------------------------------------------ job callbacks
    def _job_done(self, job: _RepairJob, report: Any) -> None:
        self._job = None
        self._set_report(report)
        self.status_label.setText("")
        if report.written and report.target is not None:
            _status(self.app, t("gui.repairdialog.status.written", path=str(report.target)))
        self._refresh()
        self.run_finished.emit(report)

    def _job_failed(self, job: _RepairJob, exc: BaseException) -> None:
        self._job = None
        report = getattr(exc, "report", None)
        self.status_label.setText("")
        if report is not None:
            self._set_report(report)      # the problem box tells what stopped it
        else:
            self._set_error(t("gui.repairdialog.failed", error=_error_text(exc)))
            if not isinstance(exc, JsonReshapeError):
                self.app.report_error(exc, self)
        self._refresh()
        self.run_failed.emit(exc)

    def _job_cancelled(self, job: _RepairJob) -> None:
        self._job = None
        self.status_label.setText(t("gui.repairdialog.cancelled"))
        self._refresh()
        self.run_cancelled.emit()

    def _on_task_updated(self, task: Any) -> None:
        job = self._job
        if job is None or task is None or task is not job.task or task.state != "running" or self._closing:
            return
        if getattr(task, "queued", False):
            self._queued_shown = True
            self.progress_bar.setRange(0, 0)
            self.status_label.setText(t("gui.tasks.queued"))
            return
        info = task.info()
        if info is None:
            self.progress_bar.setRange(0, 0)
            return
        if info.fraction is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(max(0.0, min(1.0, info.fraction)) * 1000))
        parts = [info.message] if info.message else []
        if info.total:
            values = {"done": format_bytes(info.done), "total": format_bytes(info.total),
                      "rate": format_bytes(info.rate)}
            if info.eta is not None and info.eta > 0:
                parts.append(t("gui.repairdialog.progress_eta", eta=format_duration(info.eta), **values))
            else:
                parts.append(t("gui.repairdialog.progress", **values))
        if parts:
            self.status_label.setText(copyfmt.escape_surrogates(" — ".join(parts)))

    # ------------------------------------------------------------------ the report
    def _set_report(self, report: Any) -> None:
        self._release_report()
        lease = _Lease(report)
        self._lease = lease
        self._holder.lease = lease
        self._fill_report(report)

    def _release_report(self) -> None:
        lease, self._lease = self._lease, None
        self._holder.lease = None
        if lease is not None:
            lease.release()
        self._clear_report_ui()

    def close_report(self) -> None:
        """Forget (and close) the report shown."""
        self._release_report()
        self._refresh()

    def _clear_report_ui(self) -> None:
        if not _alive(self):
            return
        self._summary_source.rows = []
        self.summary_grid.reset()
        self._fix_source.fixes = []
        self.fixes_grid.reset()
        self.report_box.setVisible(False)

    @staticmethod
    def summary_rows_of(report: Any) -> list[tuple[str, int, Any]]:
        """``(code, count, first fix)`` per fix code, in the order of ``report.summary_lines()`` (the first
        location in the file first)."""
        codes = [code for code, n in report.counts.items() if n]
        codes.sort(key=lambda c: (report.first[c].offset if c in report.first else 0,
                                  FIX_CODES.index(c) if c in FIX_CODES else 99))
        return [(code, int(report.counts[code]), report.first.get(code)) for code in codes]

    def _fill_report(self, rep: Any) -> None:
        headline = rep.headline()
        if rep.total == 0 and rep.problem is None:
            headline += "\n" + t("repair.summary.none_check" if rep.check_only else "repair.summary.none")
        self.headline_label.setText(copyfmt.escape_surrogates(headline))
        kinds = t("gui.repairdialog.kinds", kind=_kind_name(rep.kind), output=_kind_name(rep.output_kind))
        if rep.record_path:
            kinds += " · " + t("gui.repairdialog.kinds.record_path", path=_display(format_path(tuple(rep.record_path))))
        self.kind_label.setText(kinds)
        data = rep.data_changes_line()
        self.data_label.setText(copyfmt.escape_surrogates(data or ""))
        self.data_label.setVisible(bool(data))
        self._summary_source.rows = self.summary_rows_of(rep)
        self.summary_grid.reset()
        self.summary_grid.setVisible(bool(self._summary_source.rows))
        if self._summary_source.rows:
            self.summary_grid.set_current(0, 0)
        tr = rep.truncated
        self.truncation_box.setVisible(tr is not None and rep.problem is None)
        if tr is not None:
            self.truncation_label.setText(t("gui.repairdialog.truncated.text", line=tr.line, column=tr.column,
                                            kept=format_count(tr.kept_records), dropped=format_bytes(tr.dropped_bytes),
                                            closers=tr.closers or "-"))
        p = rep.problem
        self.problem_box.setVisible(p is not None)
        if p is not None:
            self.problem_label.setText(copyfmt.escape_surrogates(p.message))
            self.context_label.setText(copyfmt.escape_surrogates(p.context()))
            lines = []
            if tr is not None:
                lines.append(t("repair.summary.partial_written" if rep.written else "repair.summary.partial",
                               kept=format_count(tr.kept_records)))
            if not rep.written and not rep.check_only:
                lines.append(t("gui.repairdialog.problem.nothing_written"))
            self.partial_label.setText("\n".join(lines))
            self.partial_label.setVisible(bool(lines))
            self.save_partial_button.setVisible(not rep.written and tr is not None)
        self._fix_source.fixes = list(rep.fixes)
        self.fixes_grid.reset()
        shown = len(self._fix_source.fixes)
        if rep.total > shown:
            note = t("gui.repairdialog.fixes.shown", shown=format_count(shown), total=format_count(rep.total))
        else:
            note = t("gui.repairdialog.fixes.title", count=format_count(shown))
        self.fixes_label.setText(note)
        warnings = rep.warning_texts()
        self.warnings_label.setText(copyfmt.escape_surrogates("\n".join(warnings)))
        self.warnings_label.setVisible(bool(warnings))
        self.report_box.setVisible(True)

    def headline_text(self) -> str:
        return self.headline_label.text()

    def summary_row_count(self) -> int:
        return self._summary_source.row_count()

    def fix_count_shown(self) -> int:
        return self._fix_source.row_count()

    # ------------------------------------------------------------------ locations
    def _show(self, loc: RepairLocation) -> RepairLocation | None:
        callback = self._show_location_cb
        if callback is None:
            self._set_note(t("gui.repairdialog.show_location.none"))
            return None
        self._set_note(None)
        try:
            callback(loc)
        except Exception as exc:  # noqa: BLE001 - a callback of the window: reported, never raised
            log.exception("show_location failed")
            self.app.report_error(exc, self)
            return None
        return loc

    def _source_of_report(self) -> str:
        rep = self.report
        return str(rep.source) if rep is not None else self.source_path()

    def activate_summary(self, row: int) -> RepairLocation | None:
        """Show the first location of the fix code in summary row ``row``."""
        rows = self._summary_source.rows
        if not 0 <= row < len(rows) or rows[row][2] is None:
            return None
        return self._show(location_of(self._source_of_report(), rows[row][2]))

    def activate_fix(self, row: int) -> RepairLocation | None:
        """Show the location of the listed fix ``row``."""
        fixes = self._fix_source.fixes
        if not 0 <= row < len(fixes):
            return None
        return self._show(location_of(self._source_of_report(), fixes[row]))

    def show_truncation(self) -> RepairLocation | None:
        """Show where the data of a truncated file ends (the cut)."""
        rep = self.report
        if rep is None or rep.truncated is None:
            return None
        return self._show(location_of(self._source_of_report(), rep.truncated, "truncated"))

    def show_problem(self) -> RepairLocation | None:
        """Show the unrepairable place."""
        rep = self.report
        if rep is None or rep.problem is None:
            return None
        return self._show(location_of(self._source_of_report(), rep.problem))

    def selected_fix_rows(self) -> list[int]:
        rows: set[int] = set()
        n = self.fix_count_shown()
        for r0, r1, _c0, _c1 in self.fixes_grid.selected_ranges():
            rows.update(range(max(0, r0), min(r1, n - 1) + 1))
        if not rows:
            cur = self.fixes_grid.current()[0]
            if 0 <= cur < n:
                rows.add(cur)
        return sorted(rows)

    def copy_fixes(self, rows: list[int] | None = None) -> str:
        """Copy listed fixes as tab-separated lines (name, line, column, offset, before, after, record)."""
        rows = self.selected_fix_rows() if rows is None else rows
        lines = []
        for row in rows:
            if not 0 <= row < len(self._fix_source.fixes):
                continue
            fx = self._fix_source.fixes[row]
            lines.append([fx.name, fx.line, fx.column, fx.offset, fx.before, fx.after,
                          "" if fx.record is None else fx.record])
        if not lines:
            return ""
        return set_clipboard_text(copyfmt.tsv_text(lines))

    def _fixes_key(self, event: QKeyEvent) -> bool:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_fixes()
            return True
        return False

    # ------------------------------------------------------------------ report actions
    def open_repaired(self) -> bool:
        """"Ta'mirlangan faylni ochish": ``open_repaired(target, report.reopen_options())`` (default:
        ``app.request_open``)."""
        rep = self.report
        if rep is None or not rep.written or rep.check_only or rep.target is None:
            return False
        path = str(rep.target)
        options = dict(rep.reopen_options())
        if self._open_cb is not None:
            self._open_cb(path, options)
            return True
        request = getattr(self.app, "request_open", None)
        if not callable(request):
            return False
        request(path, options)
        return True

    def save_report(self, path: str | os.PathLike[str] | None = None, fmt: str | None = None) -> Any:
        """Write every fix of the report shown (``report.write``) to ``path`` (asked when ``None``;
        ``fmt`` ``csv`` | ``json`` | ``jsonl``, default by the extension) in a task. Returns the task."""
        lease = self._lease
        rep = lease.report if lease is not None else None
        if lease is None or rep is None:
            self._set_error(t("gui.repairdialog.save_report.none"))
            return None
        if path is None:
            path = self._ask_save(self, t("gui.repairdialog.save_report.title"), default_report_path(rep.source),
                                  t("gui.repairdialog.save_report.filter"))
            if not path:
                return None
        out = os.path.abspath(os.fspath(path))
        ext = os.path.splitext(out)[1].lower()
        kind = (fmt or REPORT_FORMATS.get(ext) or "").lower().lstrip(".")
        if not kind and not ext:
            out += ".csv"
            kind = "csv"
        try:
            if kind not in ("csv", "json", "jsonl"):
                raise UserInputError("gui.repairdialog.save_report.bad_format", name=os.path.basename(out))
            check_not_source(out, (rep.source, rep.target))
        except UserInputError as exc:
            self._set_error(exc.message)
            return None
        self._set_error(None)
        # a file another tab shows: refused while it has changes, else asked; that tab reloads it (review S3)
        reload = open_target_check(self, out, on_refused=self._set_error)
        if reload is None:
            return None
        report = lease.acquire()
        app = self.app
        dialog_ref = weakref.ref(self)

        def work(_ctx: Any) -> str:
            report.write(out, kind)
            return out

        def alive_dialog() -> RepairDialog | None:
            d = dialog_ref()
            return d if _alive(d) and not d._closing else None

        def done(_result: Any) -> None:
            lease.done_using()
            reload_tabs(reload)
            text = t("gui.repairdialog.save_report.done", path=out)
            _status(app, text)
            d = alive_dialog()
            if d is not None:
                d.status_label.setText(copyfmt.escape_surrogates(text))
                d.report_saved.emit(out)

        def failed(exc: BaseException) -> None:
            lease.done_using()
            d = alive_dialog()
            if d is not None:
                d._set_error(t("gui.repairdialog.failed", error=_error_text(exc)))
            if d is None or not isinstance(exc, JsonReshapeError):
                app.report_error(exc, d)

        return app.tasks.run(work, title=t("gui.repairdialog.save_report.task", name=os.path.basename(out)),
                             kind="export", owner=_TaskOwner(), on_done=done, on_error=failed,
                             on_cancel=lease.done_using)

    # ------------------------------------------------------------------ state
    def _refresh(self) -> None:
        if not _alive(self):
            return
        running = self.running
        has_source = bool(self.source_path())
        self.check_button.setEnabled(has_source and not running and not self._closing)
        self.repair_button.setEnabled(has_source and bool(self.target_path()) and not running and not self._closing)
        self.stop_button.setVisible(running)
        self.progress_bar.setVisible(running)
        if running and not self._queued_shown:
            self.progress_bar.setRange(0, 0)
        for widget in (self.options_box, self.source_edit, self.source_button, self.target_edit,
                       self.target_button):
            widget.setEnabled(not running)
        rep = self.report
        self.report_box.setVisible(rep is not None)
        self.save_report_button.setEnabled(rep is not None and rep.total > 0 and not running)
        self.open_button.setEnabled(rep is not None and bool(rep.written) and not rep.check_only
                                    and rep.target is not None and not running)
        self.save_partial_button.setEnabled(not running)
        can_show = self._show_location_cb is not None
        tip = t("gui.repairdialog.show_location.tip") if can_show else t("gui.repairdialog.show_location.none")
        for button in (self.truncation_button, self.problem_button):
            button.setEnabled(can_show)
            button.setToolTip(tip)

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt name
        self._closing = False
        super().showEvent(event)
        self._refresh()

    def done(self, result: int) -> None:  # noqa: D401 - Qt name
        """Close the window: a running check / repair is stopped after a confirmation (declined: the
        window stays open); the report shown is closed."""
        if not self._closing and self.running:
            if not confirm(self, t("gui.repairdialog.confirm_stop")):
                return
            self.cancel_run()
        self._closing = True
        self._job = None              # a late result closes its own report
        self._release_report()
        super().done(result)


def show_repair_dialog(app: Any, source: str | os.PathLike[str] | None = None, parent: QWidget | None = None,
                       **kwargs: Any) -> RepairDialog:
    """Tools ▸ "JSON'ni ta'mirlash…" / the "Ta'mirlash…" buttons of a tab: the repair window of
    ``source`` (an open one is brought to the front), shown non-modal and deleted when closed."""
    key = normalize_path(source) if source else None
    for k in [k for k, ref in _OPEN.items() if not _alive(ref())]:
        del _OPEN[k]
    if key is not None:
        ref = _OPEN.get(key)
        dialog = ref() if ref is not None else None
        if dialog is not None and _alive(dialog) and dialog.isVisible() and not dialog._closing:
            dialog.raise_()
            dialog.activateWindow()
            return dialog
    dialog = RepairDialog(app, source, parent, **kwargs)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    if parent is None:
        keep_alive(dialog)
    if key is not None:
        _OPEN[key] = weakref.ref(dialog)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
