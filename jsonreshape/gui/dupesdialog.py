"""Duplicate records dialog (docs/ARCHITECTURE.md §14.9; design: scratchpad ``s5/map_stats.md`` §7.1;
AGENT_PROMPT §9 "Dublikatlar": count and list by a chosen key).

:class:`DupesDialog` (non-modal, one per session -- :func:`show_dupes_dialog` raises the open one) finds the
records whose key value occurs more than once with :func:`jsonreshape.dupes.find_duplicates`:

* **Inputs**: the key -- an editable combo listing the schema fields that are not under an array (wildcard
  keys are refused by the finder: ``*`` / ``[*]`` / ``..`` are never listed and a typed one shows the
  finder's own error under the combo), ``$key`` for documents whose records are object members, and ``$``
  (the whole record); keep the first / the last record of each group (position order); the scope: the
  current view or all records (single documents: their one record).
* **Run**: a *heavy* task (kind ``dupes``, owned by the session: closing or reopening the file cancels it)
  on ``session.snapshot()`` taken when the user asks (the view set leased with ``keep_document_for``),
  with progress and Stop.
* **Result**: the counts (records checked, with / without the key, unreadable, distinct key values =
  ``records_with_key - duplicates``, repeated values = groups, extra copies = duplicates), the warnings
  (``DuplicateReport.warning_texts()``) and the first 20 groups (key text, size, up to 20 record numbers
  ``#n``, 0-based; the kept record marked when it is listed). Activating a record (Enter / double click)
  shows it in the tree -- the key node for a path key, the record for ``$`` / ``$key``; a group's
  "Filtr" (button, context menu) filters the view by ``key == value``: the value is the group's key text
  (compact JSON, exact) or, when that text was cut at 120 characters, read again from the group's first
  record in a silent task; it is applied with :func:`jsonreshape.gui.statspanel.apply_filter`.
* **Actions**: "Takroriylarni ko'rsatish" copies the duplicates' record set (a silent task) and shows it as
  the view (``session.show_records(copy, t("dupes.desc", key=...))``; the copy keeps the result's
  ``gen``, so a set of an older edit state is refused by the session); "Eksport…" hands such a copy and
  its label to the ``open_export(records, label)`` callback (default: show the copy as the view and open
  ``ExportDialog(session, parent, scope="view")`` -- the export dialog has no record-set scope yet);
  "Takroriylarni o'chirish…" hands ``BulkSpec("dedupe", {"key", "keep"})`` and the bulk scope (``"all"``,
  or ``"view"`` when the search ran on a filtered view that is still the current one) to the
  ``open_bulk(spec, scope)`` callback (default: ``BulkDialog.show_for(session, parent=..., scope_default=
  scope)`` + ``set_spec(spec)`` + ``set_scope_choice(scope)`` + ``preview()`` -- the stage-3 preview, which
  runs the finder again; nothing changes until the user applies it there).
* **Stale**: after ``session.data_changed`` or a reopen the result is marked out of date: the record
  numbers may point to other records, so revealing, showing and exporting are disabled until the next
  search (the filter -- a value -- and the removal -- which searches again -- stay available); a view
  change after a search of the current view is noted, and the removal of that search is refused (it would
  delete by another set of records) until the search is repeated or the scope "all records" is chosen.

Decisions: the dialog never starts a search by itself (the key given by :func:`show_dupes_dialog` is only
prefilled: a search of a big file is a heavy task); ``keep="none"`` of the finder (mark every member) is not
offered -- the bulk dedupe accepts only first / last; the dupes report's record set belongs to the dialog
(released when it closes or a new result replaces it, after the copies that are being made from it).

Integration API (round B): :func:`show_dupes_dialog` ``(window, session, key=None, *, scope=None,
open_export=None, open_bulk=None) -> DupesDialog``; ``DupesDialog(session, parent=None, *, key=None,
scope=None, open_export=None, open_bulk=None)``; ``instance(session)``; ``run()``, ``stop()``,
``show_duplicates()``, ``export()``, ``remove_duplicates()``, ``filter_group(i)``, ``reveal_position(pos)``;
``report`` (the :class:`~jsonreshape.dupes.DuplicateReport` shown); signal ``report_ready(report)``.
"""

from __future__ import annotations

import logging
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Callable

import shiboken6
from PySide6.QtCore import QEvent, QPoint, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QCompleter,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import codec
from jsonreshape.bulk import BulkSpec
from jsonreshape.dupes import KEY_FIELD, DuplicateReport, find_duplicates, parse_key
from jsonreshape.errors import JsonReshapeError
from jsonreshape.gui import fields, theme
from jsonreshape.gui.searchpanel import keep_for, snapshot_recordset
from jsonreshape.gui.session import NodeRef
from jsonreshape.gui.statspanel import MenuReason, StatsFilter, apply_filter
from jsonreshape.gui.widgets import format_count, mono_font, set_clipboard_text, task_node
from jsonreshape.i18n import get_language, t
from jsonreshape.paths import MISSING
from jsonreshape.query import Condition
from jsonreshape.recordset import RecordSet
from jsonreshape.schema import field_paths

log = logging.getLogger(__name__)

__all__ = ["DupesDialog", "SCOPES", "show_dupes_dialog"]

SCOPES = ("view", "all")
KEEPS = ("first", "last")
COUNT_ROWS = ("scanned", "with_key", "missing", "skipped", "distinct", "groups", "duplicates")
SCOPE_REFRESH_MS = 300
MENU_EXPR_CHARS = 80
ROLE_KIND = int(Qt.ItemDataRole.UserRole)        # ("group", i) / ("pos", i, position) / ("more", i)
_ELLIPSIS = "…"

_OPEN: weakref.WeakKeyDictionary[Any, DupesDialog] = weakref.WeakKeyDictionary()


def _alive(obj: Any) -> bool:
    try:
        return obj is not None and shiboken6.isValid(obj)
    except Exception:  # pragma: no cover
        return False


def _call(ref: Any, name: str, *args: Any) -> None:
    obj = ref()
    if obj is not None and _alive(obj) and not getattr(obj, "_closing", True):
        getattr(obj, name)(*args)


def _count(n: int) -> str:
    return format_count(int(n))


def _tn(key: str, n: int, **params: Any) -> str:
    return t(key + ".one" if int(n) == 1 else key, **params)


def _seconds(value: float) -> str:
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") if get_language() == "uz" else text


def _elide(text: str, limit: int = MENU_EXPR_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + _ELLIPSIS


def _error_text(exc: BaseException | None) -> str:
    if isinstance(exc, JsonReshapeError):
        return exc.message
    return t("gui.widgets.error.unexpected") if exc is not None else ""


class _ReportLease:
    """The report's record set, shared by the dialog (its owner) and the tasks copying it: closed once the
    dialog released it and no task uses it any more. Thread-safe."""

    def __init__(self, report: DuplicateReport) -> None:
        self.report = report
        self._users = 0
        self._released = False
        self._lock = threading.Lock()

    def acquire(self) -> RecordSet:
        with self._lock:
            self._users += 1
        return self.report.records

    def done_using(self) -> None:
        with self._lock:
            self._users -= 1
            close = self._released and self._users <= 0
        if close:
            self.report.close()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            close = self._users <= 0
        if close:
            self.report.close()


@dataclass
class _Meta:
    """What a search covered (the report shown belongs to it)."""

    key: str
    keep: str
    scope: str
    all_records: bool          # the source was every record (scope "all", or an unfiltered view)
    data_serial: int
    view_serial: int | None
    single: bool


def show_dupes_dialog(window: QWidget | None, session: Any, key: str | None = None, *, scope: str | None = None,
                      open_export: Callable[[RecordSet, str], Any] | None = None,
                      open_bulk: Callable[[BulkSpec, str], Any] | None = None) -> DupesDialog:
    """Show the duplicates dialog of ``session`` (created on first use, else raised: one per session);
    ``key`` / ``scope`` prefill the inputs (not while a search runs); the callbacks replace the defaults
    (module docstring)."""
    dlg = DupesDialog.instance(session)
    if dlg is None:
        dlg = DupesDialog(session, window, key=key, scope=scope, open_export=open_export, open_bulk=open_bulk)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    else:
        if open_export is not None:
            dlg.open_export = open_export
        if open_bulk is not None:
            dlg.open_bulk = open_bulk
        if dlg.task is None:
            if key is not None:
                dlg.set_key(key)
            if scope is not None:
                dlg.set_scope(scope)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    return dlg


class DupesDialog(QDialog):
    """Find, list, show, export and remove duplicate records (module docstring)."""

    report_ready = Signal(object)    # the DuplicateReport shown

    def __init__(self, session: Any, parent: QWidget | None = None, *, key: str | None = None,
                 scope: str | None = None, open_export: Callable[[RecordSet, str], Any] | None = None,
                 open_bulk: Callable[[BulkSpec, str], Any] | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DupesDialog")
        self.session = session
        self.app = session.app
        #: ``fn(records, label)``: export the duplicates (the callee owns ``records``); ``None``: default
        self.open_export = open_export
        #: ``fn(spec, scope)``: open the bulk dedupe preview; ``None``: default (BulkDialog)
        self.open_bulk = open_bulk
        self.report: DuplicateReport | None = None
        self.task: Any = None
        self.export_dialog: Any = None       # the export dialog the default ``open_export`` opened
        self.bulk_dialog: Any = None         # the bulk dialog the default ``open_bulk`` opened
        self._lease: _ReportLease | None = None
        self._meta: _Meta | None = None
        self._pending: _Meta | None = None
        self._req = 0
        self._data_serial = 0
        self._stale: str | None = None       # None | "data" | "view"
        self._own_view_serial: int | None = None
        self._showing = False                # show_records of our own copy is running
        self._status = "idle"                # idle | running | done | cancelled | failed
        self._message = ""
        self._message_kind = "info"
        self._error: BaseException | None = None
        self._closing = False
        self._side_tasks: list[Any] = []     # copy / value tasks (cancelled when the dialog closes)
        self.last_menu: QMenu | None = None  # the context menu shown last (tests, automation)
        self.setWindowTitle(t("gui.dupesdialog.title", name=getattr(session, "name", "")))
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setSizeGripEnabled(True)
        self._scope_timer = QTimer(self)
        self._scope_timer.setSingleShot(True)
        self._scope_timer.setInterval(SCOPE_REFRESH_MS)
        self._scope_timer.timeout.connect(self._update_scope_texts)
        self._build()
        for name, slot in (("state_changed", self._on_session_state), ("data_changed", self._on_data_changed),
                           ("view_changed", self._on_view_changed), ("count_changed", self._on_count_changed),
                           ("index_finished", self._on_index_finished), ("view_busy_changed", self._on_busy)):
            signal = getattr(session, name, None)
            if signal is not None and hasattr(signal, "connect"):
                signal.connect(slot)
        self.app.tasks.task_updated.connect(self._on_task_updated)
        theme.notifier().theme_changed.connect(self._on_theme)
        try:
            _OPEN[session] = self
        except TypeError:  # pragma: no cover - a session that cannot be weakly referenced
            pass
        if key is not None:
            self.set_key(key)
        doc = getattr(session, "doc", None)
        single = doc is not None and getattr(doc, "kind", None) == "single"
        self.set_scope(scope if scope in SCOPES else ("all" if single else "view"))
        self._load_fields()
        self._on_theme()
        self._validate_key()
        self._refresh()
        self.resize(640, 620)

    # ================================================================================== registry
    @classmethod
    def instance(cls, session: Any) -> DupesDialog | None:
        """The open dialog of ``session`` (``None`` when there is none)."""
        try:
            dlg = _OPEN.get(session)
        except TypeError:  # pragma: no cover
            return None
        return dlg if _alive(dlg) and not dlg._closing else None

    # ================================================================================== build
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.key_combo = QComboBox(self)
        self.key_combo.setObjectName("dupes_key")
        self.key_combo.setEditable(True)
        self.key_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.key_combo.setMaxVisibleItems(20)
        self.key_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.key_combo.setToolTip(t("gui.dupesdialog.key.tip"))
        self.key_combo.setAccessibleName(t("gui.dupesdialog.key.name"))
        edit = self.key_combo.lineEdit()
        edit.setPlaceholderText(t("gui.dupesdialog.key.placeholder"))
        edit.setClearButtonEnabled(True)
        edit.textChanged.connect(self._on_key_changed)
        edit.returnPressed.connect(self._on_return)
        self.key_model = QStringListModel([], self)
        self.completer = QCompleter(self.key_model, self)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.key_combo.setCompleter(self.completer)
        form.addRow(t("gui.dupesdialog.key.label"), self.key_combo)
        self.key_error = QLabel(self)
        self.key_error.setTextFormat(Qt.TextFormat.PlainText)
        self.key_error.setWordWrap(True)
        self.key_error.setVisible(False)
        form.addRow("", self.key_error)

        keep_row = QHBoxLayout()
        self.keep_group = QButtonGroup(self)
        self.keep_buttons: dict[str, QRadioButton] = {}
        for i, keep in enumerate(KEEPS):
            button = QRadioButton(t(f"gui.dupesdialog.keep.{keep}"), self)
            button.setObjectName(f"dupes_keep_{keep}")
            button.setToolTip(t("gui.dupesdialog.keep.tip"))
            self.keep_group.addButton(button, i)
            self.keep_buttons[keep] = button
            keep_row.addWidget(button)
        keep_row.addStretch(1)
        self.keep_buttons["first"].setChecked(True)
        form.addRow(t("gui.dupesdialog.keep.label"), keep_row)

        scope_row = QHBoxLayout()
        self.scope_group = QButtonGroup(self)
        self.scope_buttons: dict[str, QRadioButton] = {}
        for i, scope in enumerate(SCOPES):
            button = QRadioButton(self)
            button.setObjectName(f"dupes_scope_{scope}")
            self.scope_group.addButton(button, i)
            self.scope_buttons[scope] = button
            scope_row.addWidget(button)
        scope_row.addStretch(1)
        self.scope_buttons["view"].setChecked(True)
        self.scope_group.idToggled.connect(lambda _id, _on: self._refresh_buttons())
        form.addRow(t("gui.dupesdialog.scope.label"), scope_row)
        layout.addLayout(form)

        run_row = QHBoxLayout()
        self.run_button = QPushButton(t("gui.dupesdialog.run"), self)
        self.run_button.setObjectName("dupes_run")
        self.run_button.setToolTip(t("gui.dupesdialog.run.tip"))
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(lambda _checked=False: self.run())
        run_row.addWidget(self.run_button)
        self.stop_button = QPushButton(t("gui.dupesdialog.stop"), self)
        self.stop_button.setObjectName("dupes_stop")
        self.stop_button.setAutoDefault(False)
        self.stop_button.clicked.connect(lambda _checked=False: self.stop())
        self.stop_button.setVisible(False)
        run_row.addWidget(self.stop_button)
        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        run_row.addWidget(self.progress_bar, 1)
        layout.addLayout(run_row)

        self.status_label = QLabel(self)
        self.status_label.setObjectName("dupes_status")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status_label)
        self.stale_label = QLabel(self)
        self.stale_label.setObjectName("dupes_stale")
        self.stale_label.setTextFormat(Qt.TextFormat.PlainText)
        self.stale_label.setWordWrap(True)
        self.stale_label.setVisible(False)
        layout.addWidget(self.stale_label)

        counts = QFormLayout()
        counts.setHorizontalSpacing(12)
        self.count_labels: dict[str, QLabel] = {}
        for name in COUNT_ROWS:
            value = QLabel(self)
            value.setObjectName(f"dupes_count_{name}")
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.count_labels[name] = value
            counts.addRow(t(f"gui.dupesdialog.count.{name}"), value)
        self.counts_box = QWidget(self)
        self.counts_box.setLayout(counts)
        self.counts_box.setVisible(False)
        layout.addWidget(self.counts_box)

        self.warnings_label = QLabel(self)
        self.warnings_label.setObjectName("dupes_warnings")
        self.warnings_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warnings_label.setWordWrap(True)
        self.warnings_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.warnings_label.setVisible(False)
        layout.addWidget(self.warnings_label)

        self.groups_title = QLabel(self)
        self.groups_title.setTextFormat(Qt.TextFormat.PlainText)
        self.groups_title.setWordWrap(True)
        layout.addWidget(self.groups_title)
        self.groups = QTreeWidget(self)
        self.groups.setObjectName("dupes_groups")
        self.groups.setColumnCount(2)
        self.groups.setHeaderLabels([t("gui.dupesdialog.groups.col.key"), t("gui.dupesdialog.groups.col.size")])
        self.groups.setAccessibleName(t("gui.dupesdialog.groups.name"))
        self.groups.setRootIsDecorated(True)
        self.groups.setUniformRowHeights(True)
        self.groups.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.groups.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.groups.customContextMenuRequested.connect(self._on_groups_menu)
        self.groups.itemActivated.connect(self._on_item_activated)
        self.groups.currentItemChanged.connect(lambda *_args: self._refresh_buttons())
        header = self.groups.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        try:
            self.groups.setFont(mono_font(self.session))
        except Exception:  # pragma: no cover - font problems never break the dialog
            log.debug("could not set the group font", exc_info=True)
        self._menu_reason = MenuReason(self)
        self.groups.installEventFilter(self._menu_reason)
        self.groups.viewport().installEventFilter(self._menu_reason)
        copy = QAction(self.groups)
        copy.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))
        copy.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        copy.triggered.connect(lambda _checked=False: self._copy_current())
        self.groups.addAction(copy)
        layout.addWidget(self.groups, 1)

        actions = QHBoxLayout()
        self.filter_button = QPushButton(t("gui.dupesdialog.filter"), self)
        self.filter_button.setObjectName("dupes_filter")
        self.filter_button.setToolTip(t("gui.dupesdialog.filter.tip"))
        self.filter_button.setAutoDefault(False)
        self.filter_button.clicked.connect(lambda _checked=False: self.filter_group(self.current_group()))
        actions.addWidget(self.filter_button)
        self.show_button = QPushButton(t("gui.dupesdialog.show"), self)
        self.show_button.setObjectName("dupes_show")
        self.show_button.setAutoDefault(False)
        self.show_button.clicked.connect(lambda _checked=False: self.show_duplicates())
        actions.addWidget(self.show_button)
        self.export_button = QPushButton(t("gui.dupesdialog.export"), self)
        self.export_button.setObjectName("dupes_export")
        self.export_button.setAutoDefault(False)
        self.export_button.clicked.connect(lambda _checked=False: self.export())
        actions.addWidget(self.export_button)
        self.remove_button = QPushButton(t("gui.dupesdialog.remove"), self)
        self.remove_button.setObjectName("dupes_remove")
        self.remove_button.setAutoDefault(False)
        self.remove_button.clicked.connect(lambda _checked=False: self.remove_duplicates())
        actions.addWidget(self.remove_button)
        actions.addStretch(1)
        self.close_button = QPushButton(t("gui.dupesdialog.close"), self)
        self.close_button.setObjectName("dupes_close")
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

    def _on_theme(self) -> None:
        if self._closing or not _alive(self):
            return
        self.key_error.setStyleSheet(f"color: {theme.color('danger').name()};")
        self.stale_label.setStyleSheet(f"color: {theme.color('warn').name()};")
        self.warnings_label.setStyleSheet(f"color: {theme.color('warn').name()};")
        self._style_status()

    def _style_status(self) -> None:
        kind = self._message_kind if self._message else ("error" if self._status == "failed" else "info")
        role = {"error": "danger", "warning": "warn", "ok": "ok"}.get(kind)
        self.status_label.setStyleSheet(f"color: {theme.color(role).name()};" if role else "")

    # ================================================================================== inputs
    def key_text(self) -> str:
        return self.key_combo.currentText().strip()

    def set_key(self, key: str) -> None:
        self.key_combo.setEditText(str(key))

    def key_items(self) -> list[str]:
        return [self.key_combo.itemText(i) for i in range(self.key_combo.count())]

    def keep(self) -> str:
        for keep, button in self.keep_buttons.items():
            if button.isChecked():
                return keep
        return "first"

    def set_keep(self, keep: str) -> None:
        if keep not in KEEPS:
            raise ValueError(f"unknown keep: {keep!r}")
        self.keep_buttons[keep].setChecked(True)

    def scope(self) -> str:
        for scope, button in self.scope_buttons.items():
            if button.isChecked():
                return scope
        return "view"

    def set_scope(self, scope: str) -> None:
        if scope not in SCOPES:
            raise ValueError(f"unknown scope: {scope!r}")
        self.scope_buttons[scope].setChecked(True)

    def _load_fields(self) -> None:
        session = self.session
        doc = getattr(session, "doc", None)
        if doc is None or not getattr(doc, "index_complete", False):
            return
        ref = weakref.ref(self)
        try:
            schema = fields.get_schema(session, lambda sch: _call(ref, "_fields_ready", sch))
        except Exception:  # pragma: no cover - the key list is a convenience
            log.debug("the schema for the key list is not available", exc_info=True)
            return
        if schema is not None:
            self._fields_ready(schema)

    def _fields_ready(self, schema: Any) -> None:
        doc = getattr(self.session, "doc", None)
        items: list[str] = []
        if doc is not None and getattr(doc, "member_records", False) and getattr(doc, "kind", None) != "single":
            items.append(KEY_FIELD)
        for text in field_paths(schema, inside_arrays=False):
            if text not in items:
                items.append(text)
        if "$" not in items:
            items.append("$")
        text = self.key_combo.currentText()
        self.key_combo.blockSignals(True)
        try:
            self.key_combo.clear()
            self.key_combo.addItems(items)
            self.key_combo.setEditText(text)
        finally:
            self.key_combo.blockSignals(False)
        self.key_model.setStringList(items)

    def _on_key_changed(self, _text: str = "") -> None:
        self._validate_key()
        self._refresh_buttons()

    def _validate_key(self) -> str | None:
        text = self.key_text()
        if not text:
            self.key_error.setVisible(False)
            return None
        try:
            key, _path = parse_key(text)
        except JsonReshapeError as exc:
            self.key_error.setText(exc.message)
            self.key_error.setVisible(True)
            return None
        self.key_error.setVisible(False)
        return key

    def _on_return(self) -> None:
        if self.completer.popup() is not None and self.completer.popup().isVisible():
            return
        self.run()

    def _update_scope_texts(self) -> None:
        if self._closing or not _alive(self):
            return
        session = self.session
        doc = getattr(session, "doc", None)
        total = int(doc.record_count) if doc is not None else 0
        single = doc is not None and getattr(doc, "kind", None) == "single"
        self.scope_buttons["all"].setText(t("gui.dupesdialog.scope.all", count=_count(total)))
        view = self.scope_buttons["view"]
        if bool(getattr(session, "view_is_base", True)):
            view.setText(t("gui.dupesdialog.scope.view_base"))
        else:
            try:
                n = int(session.view_len())
            except Exception:
                n = 0
            view.setText(_tn("gui.dupesdialog.scope.view", n, count=_count(n)))
        try:
            label = session.view_label()
        except Exception:
            label = ""
        view.setToolTip(label)
        view.setEnabled(not single)
        if single and view.isChecked():
            self.scope_buttons["all"].setChecked(True)

    # ================================================================================== search
    def _blocked(self) -> str:
        session = self.session
        if getattr(session, "closed", False):
            return t("gui.dupesdialog.status.no_document")
        doc = getattr(session, "doc", None)
        if doc is None or not getattr(doc, "index_complete", False):
            return t("gui.dupesdialog.status.not_ready")
        if self.scope() == "view" and getattr(doc, "kind", None) != "single" \
                and bool(getattr(session, "view_busy", False)):
            return t("gui.dupesdialog.status.view_busy")
        text = self.key_text()
        if not text:
            return t("gui.dupesdialog.key.empty")
        try:
            parse_key(text)
        except JsonReshapeError as exc:
            return exc.message
        return ""

    def run(self) -> Any:
        """Find the duplicates with the inputs (a running search is replaced). Returns the task, or
        ``None`` when it cannot start (the reason is shown)."""
        if self._closing:
            return None
        reason = self._blocked()
        if reason:
            self._set_message(reason, "warning")
            self._refresh()
            return None
        session = self.session
        key, _path = parse_key(self.key_text())
        keep = self.keep()
        single = getattr(session.doc, "kind", None) == "single"
        scope = "all" if single else self.scope()
        doc, view = session.snapshot()
        if doc is None:
            self._set_message(t("gui.dupesdialog.status.not_ready"), "warning")
            self._refresh()
            return None
        source = None
        if scope == "view" and not bool(getattr(session, "view_is_base", True)) and view is not None:
            source = view
        self._stop()
        self._req += 1
        req = self._req
        self._pending = _Meta(key=key, keep=keep, scope=scope, all_records=source is None,
                              data_serial=self._data_serial, view_serial=getattr(session, "view_serial", None),
                              single=single)
        self._status = "running"
        self._message = ""
        self._error = None
        ref = weakref.ref(self)

        def work(ctx: Any) -> DuplicateReport:
            return find_duplicates(doc, key, source, keep=keep, progress=ctx.progress, cancel=ctx.cancel)

        def done(report: DuplicateReport) -> None:
            dlg = ref()
            if dlg is None or not _alive(dlg) or dlg._closing or dlg._req != req:
                report.close()             # nobody shows it
                return
            dlg._search_done(report)

        task = self.app.tasks.run(
            work, title=t("gui.dupesdialog.task", key=_elide(key, 40), name=getattr(session, "name", "")),
            kind="dupes", owner=session, heavy=True, on_done=done,
            on_error=lambda exc: _call(ref, "_search_failed", req, exc),
            on_cancel=lambda: _call(ref, "_search_cancelled", req))
        self.task = task
        keep_for(session, task, sets=(source,) if source is not None else (), doc=doc)
        self._refresh()
        return task

    def stop(self) -> None:
        """Stop the running search."""
        task = self.task
        if task is not None and getattr(task, "state", "running") == "running":
            task.cancel()

    def _stop(self) -> None:
        task = self.task
        if task is not None and getattr(task, "state", "running") == "running":
            task.cancel()
        self.task = None
        self._req += 1

    def _search_done(self, report: DuplicateReport) -> None:
        self.task = None
        self._status = "done"
        old = self._lease
        self._lease = _ReportLease(report)
        if old is not None:
            old.release()
        self.report = report
        meta = self._pending
        self._meta = meta
        self._stale = None
        self._own_view_serial = None
        if meta is not None:
            if self._data_serial != meta.data_serial:
                self._stale = "data"
            elif meta.scope == "view" and getattr(self.session, "view_serial", None) != meta.view_serial:
                self._stale = "view"
        self._fill_groups()
        self._refresh()
        self.report_ready.emit(report)

    def _search_failed(self, req: int, exc: BaseException) -> None:
        if req != self._req:
            return
        self.task = None
        self._status = "failed"
        self._error = exc
        self._refresh()
        try:
            self.app.report_error(exc, self)
        except Exception:  # pragma: no cover
            log.exception("could not report %r", exc)

    def _search_cancelled(self, req: int) -> None:
        if req != self._req:
            return
        self.task = None
        self._status = "cancelled"
        self._refresh()

    def _on_task_updated(self, task: Any) -> None:
        if self._closing or task is not self.task or task is None:
            return
        self._update_progress(task)

    def _update_progress(self, task: Any) -> None:
        if getattr(task, "queued", False):
            self.progress_bar.setRange(0, 0)
            return
        info = task.info() if hasattr(task, "info") else None
        if info is None or info.fraction is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(max(0.0, min(1.0, info.fraction)) * 1000))

    # ================================================================================== session
    def _on_session_state(self, value: str) -> None:
        if self._closing:
            return
        if value == "closed":
            self.reject()
            return
        if value == "opening":
            # the session detached its tasks: their callbacks never come; record numbers are void
            self._req += 1
            self._data_serial += 1
            self._side_tasks = []
            if self.task is not None:
                self.task = None
                self._status = "idle"
                self._set_message(t("gui.dupesdialog.status.reopened"), "warning")
            if self.report is not None:
                self._stale = "data"
        elif value == "ready":
            self._load_fields()
        self._refresh()

    def _on_index_finished(self) -> None:
        if not self._closing:
            self._load_fields()
            self._refresh()

    def _on_data_changed(self, _cs: Any) -> None:
        if self._closing:
            return
        self._data_serial += 1
        if self.report is not None:
            self._stale = "data"
        self._refresh()

    def _on_view_changed(self) -> None:
        if self._closing:
            return
        if self._showing:                    # our own "show the duplicates": not a stale result
            self._update_scope_texts()
            return
        meta = self._meta
        if self.report is not None and meta is not None and meta.scope == "view" and self._stale is None:
            serial = getattr(self.session, "view_serial", None)
            if serial != meta.view_serial and serial != self._own_view_serial:
                self._stale = "view"
        self._update_scope_texts()
        self._refresh()

    def _on_count_changed(self) -> None:
        if not self._closing and not self._scope_timer.isActive():
            self._scope_timer.start()

    def _on_busy(self, _busy: bool) -> None:
        if not self._closing:
            self._refresh_buttons()

    # ================================================================================== display
    def _set_message(self, text: str, kind: str = "info") -> None:
        self._message = text or ""
        self._message_kind = kind

    def status(self) -> str:
        return self._status

    def stale(self) -> str | None:
        return self._stale

    def status_text(self) -> str:
        if self._message:
            return self._message
        rep = self.report
        if self._status == "running":
            return t("gui.dupesdialog.status.running")
        if self._status == "done" and rep is not None:
            return t("gui.dupesdialog.status.done", seconds=_seconds(rep.elapsed))
        if self._status == "cancelled":
            return t("gui.dupesdialog.status.cancelled")
        if self._status == "failed":
            return t("gui.dupesdialog.status.failed", error=_error_text(self._error))
        doc = getattr(self.session, "doc", None)
        if doc is None or not getattr(doc, "index_complete", False):
            return t("gui.dupesdialog.status.not_ready")
        return t("gui.dupesdialog.status.idle")

    def _refresh(self) -> None:
        if self._closing or not _alive(self):
            return
        self._update_scope_texts()
        self.status_label.setText(self.status_text())
        self._style_status()
        running = self._status == "running"
        self.progress_bar.setVisible(running)
        if running and self.task is not None:
            self._update_progress(self.task)
        if self._stale:
            self.stale_label.setText(t("gui.dupesdialog.stale.data" if self._stale == "data"
                                       else "gui.dupesdialog.stale.view"))
        self.stale_label.setVisible(bool(self._stale))
        rep = self.report
        self.counts_box.setVisible(rep is not None)
        if rep is not None:
            values = {"scanned": rep.scanned, "with_key": rep.records_with_key, "missing": rep.missing,
                      "skipped": rep.skipped, "distinct": max(0, rep.records_with_key - rep.duplicates),
                      "groups": rep.groups, "duplicates": rep.duplicates}
            for name, label in self.count_labels.items():
                label.setText(_count(values[name]))
            warnings = rep.warning_texts()
            self.warnings_label.setText("\n".join(warnings))
            self.warnings_label.setVisible(bool(warnings))
        else:
            self.warnings_label.setVisible(False)
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        if self._closing or not _alive(self):
            return
        running = self._status == "running"
        reason = self._blocked()
        self.run_button.setVisible(not running)
        self.stop_button.setVisible(running)
        self.run_button.setEnabled(not reason)
        self.run_button.setToolTip(reason or t("gui.dupesdialog.run.tip"))
        self.key_combo.setEnabled(not running)
        for button in (*self.keep_buttons.values(), *self.scope_buttons.values()):
            button.setEnabled(not running and (button is not self.scope_buttons["view"] or not self._single()))
        why = self._records_why()
        for button, tip in ((self.show_button, "gui.dupesdialog.show.tip"),
                            (self.export_button, "gui.dupesdialog.export.tip")):
            button.setEnabled(not why)
            button.setToolTip(why or t(tip))
        why = self._remove_why()
        self.remove_button.setEnabled(not why)
        self.remove_button.setToolTip(why or t("gui.dupesdialog.remove.tip"))
        why = self._filter_why(self.current_group())
        self.filter_button.setEnabled(not why)
        self.filter_button.setToolTip(why or t("gui.dupesdialog.filter.tip"))

    def _single(self) -> bool:
        doc = getattr(self.session, "doc", None)
        return doc is not None and getattr(doc, "kind", None) == "single"

    def _records_why(self) -> str:
        """Why the duplicates cannot be shown / exported now (``""``: they can)."""
        rep = self.report
        if rep is None:
            return t("gui.dupesdialog.why.no_result")
        if self._stale == "data":
            return t("gui.dupesdialog.why.stale")
        if rep.duplicates <= 0:
            return t("gui.dupesdialog.why.none")
        if self._single():
            return t("gui.dupesdialog.why.single")
        return ""

    def _remove_why(self) -> str:
        rep = self.report
        meta = self._meta
        if rep is None or meta is None:
            return t("gui.dupesdialog.why.no_result")
        if self._status == "running":
            return t("gui.dupesdialog.status.running")
        if rep.duplicates <= 0:
            return t("gui.dupesdialog.why.none")
        if meta.single:
            return t("gui.dupesdialog.why.single")
        if not meta.all_records and (getattr(self.session, "view_serial", None) != meta.view_serial
                                     or self._data_serial != meta.data_serial):
            return t("gui.dupesdialog.remove.view_changed")
        return ""

    def _filter_why(self, index: int | None) -> str:
        rep = self.report
        if rep is None:
            return t("gui.dupesdialog.why.no_result")
        if index is None or not 0 <= index < len(rep.examples):
            return t("gui.dupesdialog.why.no_group")
        if self._single():
            return t("gui.dupesdialog.why.single")
        text = rep.examples[index][0]
        if (not text or text.endswith(_ELLIPSIS)) and self._stale == "data":
            return t("gui.dupesdialog.why.stale")   # the value would be read from a record number of the old state
        return ""

    def _fill_groups(self) -> None:
        tree = self.groups
        tree.clear()
        rep = self.report
        if rep is None:
            self.groups_title.setText("")
            return
        n = len(rep.examples)
        self.groups_title.setText(_tn("gui.dupesdialog.groups.title", n, count=n) if n
                                  else t("gui.dupesdialog.groups.none"))
        for i, (text, positions) in enumerate(rep.examples):
            size = rep.example_sizes[i] if i < len(rep.example_sizes) else len(positions)
            group = QTreeWidgetItem([text, _count(size)])
            group.setData(0, ROLE_KIND, ("group", i))
            group.setToolTip(0, text)
            group.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            kept = None
            if positions:
                if rep.keep == "first":
                    kept = positions[0]
                elif rep.keep == "last" and size == len(positions):
                    kept = positions[-1]
            for pos in positions:
                label = f"#{pos}"
                if pos == kept:
                    label += " " + t("gui.dupesdialog.groups.kept")
                child = QTreeWidgetItem([label, ""])
                child.setData(0, ROLE_KIND, ("pos", i, int(pos)))
                group.addChild(child)
            if size > len(positions):
                more = QTreeWidgetItem([_tn("gui.dupesdialog.groups.more", size - len(positions),
                                            count=_count(size - len(positions))), ""])
                more.setData(0, ROLE_KIND, ("more", i))
                more.setFlags(Qt.ItemFlag.ItemIsEnabled)
                group.addChild(more)
            tree.addTopLevelItem(group)
        if n:
            tree.setCurrentItem(tree.topLevelItem(0))

    # ================================================================================== groups API
    def group_count(self) -> int:
        return self.groups.topLevelItemCount()

    def group_key_text(self, index: int) -> str:
        return self.report.examples[index][0] if self.report is not None else ""

    def group_positions(self, index: int) -> list[int]:
        return list(self.report.examples[index][1]) if self.report is not None else []

    def group_size(self, index: int) -> int:
        rep = self.report
        if rep is None:
            return 0
        return rep.example_sizes[index] if index < len(rep.example_sizes) else len(rep.examples[index][1])

    def count_text(self, name: str) -> str:
        return self.count_labels[name].text()

    def current_group(self) -> int | None:
        item = self.groups.currentItem()
        if item is None:
            return None
        data = item.data(0, ROLE_KIND)
        return int(data[1]) if isinstance(data, tuple) and len(data) > 1 else None

    def _on_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, ROLE_KIND)
        if not isinstance(data, tuple):
            return
        if data[0] == "pos":
            self.reveal_position(int(data[2]))
        elif data[0] == "group":
            positions = self.group_positions(int(data[1]))
            if positions:
                self.reveal_position(positions[0])

    def group_menu(self, item: QTreeWidgetItem) -> QMenu | None:
        """The context menu of a group or record row (the caller shows it)."""
        data = item.data(0, ROLE_KIND)
        if not isinstance(data, tuple) or self.report is None:
            return None
        index = int(data[1])
        menu = QMenu(self)
        menu.setObjectName("dupes_group_menu")
        menu.setToolTipsVisible(True)
        position = int(data[2]) if data[0] == "pos" else (self.group_positions(index) or [None])[0]
        reveal = menu.addAction(t("gui.dupesdialog.menu.reveal"))
        why = self._reveal_why()
        if why or position is None:
            reveal.setEnabled(False)
            reveal.setToolTip(why)
        else:
            reveal.triggered.connect(lambda _checked=False, p=position: self.reveal_position(p))
        filt = menu.addAction(t("gui.dupesdialog.menu.filter_key"))
        why = self._filter_why(index)
        if why:
            filt.setEnabled(False)
            filt.setToolTip(why)
        else:
            filt.setToolTip(self.group_key_text(index))
            filt.triggered.connect(lambda _checked=False, i=index: self.filter_group(i))
        menu.addSeparator()
        copy = menu.addAction(t("gui.dupesdialog.menu.copy"))
        copy.triggered.connect(lambda _checked=False, i=index: self._copy(self.group_key_text(i)))
        return menu

    def _on_groups_menu(self, pos: QPoint) -> None:
        keyboard = self._menu_reason.keyboard
        self._menu_reason.keyboard = False
        item = None if keyboard else self.groups.itemAt(pos)
        if item is None:   # the context menu key (or no row under the mouse): the current row
            item = self.groups.currentItem()
            if item is not None:
                pos = self.groups.visualItemRect(item).center()
        if item is None:
            return
        menu = self.group_menu(item)
        if menu is not None:
            menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self.last_menu = menu
            menu.popup(self.groups.viewport().mapToGlobal(pos))

    def _copy_current(self) -> None:
        index = self.current_group()
        if index is not None and self.report is not None:
            self._copy(self.group_key_text(index))

    def _copy(self, text: str) -> None:
        set_clipboard_text(text)
        show = getattr(self.app, "show_status", None)
        if callable(show):
            show(t("gui.dupesdialog.copied"), 3000)

    def _flash(self, text: str) -> None:
        self._set_message(text, "warning")
        self._refresh()

    # ================================================================================== reveal / filter
    def _key_path(self) -> tuple | None:
        """The key's path inside the record (``()`` for ``$``; ``None`` for ``$key``)."""
        rep = self.report
        if rep is None:
            return ()
        return parse_key(rep.key)[1]

    def _reveal_why(self) -> str:
        if self.report is None:
            return t("gui.dupesdialog.why.no_result")
        if self._stale == "data":
            return t("gui.dupesdialog.why.stale")
        return ""

    def reveal_position(self, position: int) -> NodeRef | None:
        """Show record ``position`` of the result in the tree (the key node for a path key)."""
        why = self._reveal_why()
        session = self.session
        if why or getattr(session, "closed", False) or getattr(session, "doc", None) is None:
            if why:
                self._flash(why)
            return None
        path = self._key_path()
        record = None if self._single() else int(position)
        ref = NodeRef(record, tuple(path or ()))
        session.select(ref, reveal=True)
        return ref

    def filter_group(self, index: int | None) -> Any:
        """Filter the view by the key value of example group ``index`` (``key == value``): True / False,
        or the silent task reading a value whose text was cut."""
        why = self._filter_why(index)
        rep = self.report
        if why or rep is None or index is None:
            if why:
                self._flash(why)
            return False
        text, positions = rep.examples[index]
        key = rep.key
        if text and not text.endswith(_ELLIPSIS):
            try:
                value = codec.loads(text)
            except JsonReshapeError:
                value = MISSING
            if value is not MISSING:
                return self._apply_value(key, value)
        if not positions:
            return False
        session = self.session
        doc = session.snapshot()[0]
        if doc is None:
            return False
        position = int(positions[0])
        path = self._key_path()
        single = self._single()
        ref = weakref.ref(self)

        def work(ctx: Any) -> Any:
            if path is None:
                return doc.record_key(position)
            node = task_node(session, NodeRef(None if single else position, tuple(path)), ctx.cancel, doc=doc)
            return node.value()

        def done(value: Any) -> None:
            if value is not None or path is not None:
                _call(ref, "_apply_value", key, value)

        app = self.app
        task = app.tasks.run(work, title=t("gui.dupesdialog.task.value"), kind="value", owner=session,
                             silent=True, on_done=done, on_error=lambda exc: app.report_error(exc))
        self._add_side_task(task)
        return task

    def _add_side_task(self, task: Any) -> None:
        self._side_tasks = [x for x in self._side_tasks if getattr(x, "state", "running") == "running"]
        self._side_tasks.append(task)

    def _apply_value(self, key: str, value: Any) -> bool:
        try:
            cond = Condition(key, "eq", value)
            flt = StatsFilter(key, cond, cond.to_text())
            ok = apply_filter(self.session, flt)
        except (JsonReshapeError, ValueError, TypeError) as exc:
            self.app.report_error(exc if isinstance(exc, JsonReshapeError) else ValueError(str(exc)), self)
            return False
        if ok:
            show = getattr(self.app, "show_status", None)
            if callable(show):
                show(t("gui.dupesdialog.filter.applied", expr=_elide(flt.text)), 4000)
        return ok

    # ================================================================================== actions
    def _copy_records(self, then: Callable[[RecordSet, str], Any]) -> Any:
        """Copy the duplicates' record set in a silent task, then ``then(copy, label)`` (GUI thread)."""
        why = self._records_why()
        lease = self._lease
        rep = self.report
        if why or lease is None or rep is None:
            if why:
                self._flash(why)
            return None
        session = self.session
        label = t("dupes.desc", key=rep.key)
        gen = rep.records.gen
        src = lease.acquire()
        ref = weakref.ref(self)

        def work(ctx: Any) -> RecordSet:
            try:
                copy = snapshot_recordset(src, ctx.cancel, kind="duplicates", description=label)
            finally:
                lease.done_using()
            copy.gen = gen
            return copy

        def done(copy: RecordSet) -> None:
            dlg = ref()
            if dlg is None or not _alive(dlg) or dlg._closing or getattr(session, "closed", False) \
                    or getattr(session, "doc", None) is None:
                copy.close()
                return
            then(copy, label)

        app = self.app
        task = app.tasks.run(work, title=t("gui.dupesdialog.task.copy"), kind="records", owner=session,
                             silent=True, on_done=done, on_error=lambda exc: app.report_error(exc))
        self._add_side_task(task)
        return task

    def show_duplicates(self) -> Any:
        """Show the duplicates (every member of a group except the kept one) as the view. Returns the copy
        task (``None`` when not possible: the reason is shown)."""
        return self._copy_records(self._show_copy)

    def _show_copy(self, copy: RecordSet, label: str) -> None:
        session = self.session
        self._showing = True
        try:
            session.show_records(copy, label)
        finally:
            self._showing = False
        self._own_view_serial = getattr(session, "view_serial", None)
        self._refresh()
        show = getattr(self.app, "show_status", None)
        if callable(show):
            show(_tn("gui.dupesdialog.shown", len(copy), count=_count(len(copy))), 4000)

    def export(self) -> Any:
        """Export the duplicates: ``open_export(records, label)`` with a copy of their record set. Returns
        the copy task."""
        return self._copy_records(self._export_copy)

    def _export_copy(self, copy: RecordSet, label: str) -> Any:
        if self.open_export is not None:
            return self.open_export(copy, label)
        return self._default_export(copy, label)

    def _default_export(self, copy: RecordSet, label: str) -> Any:
        """Show the duplicates as the view and open the export dialog on it (the export dialog has no
        record-set scope yet)."""
        self._show_copy(copy, label)
        from jsonreshape.gui.exportdialog import ExportDialog

        parent = self.parentWidget() or self
        dialog = ExportDialog(self.session, parent, scope="view")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        dialog.raise_()
        self.export_dialog = dialog
        return dialog

    def remove_duplicates(self) -> Any:
        """Open the bulk dedupe preview for this search's key, keep and scope (``open_bulk(spec, scope)``).
        Returns what the callback returned (the default: the BulkDialog) or ``None`` (the reason is
        shown)."""
        why = self._remove_why()
        rep = self.report
        meta = self._meta
        if why or rep is None or meta is None:
            if why:
                self._flash(why)
            return None
        spec = BulkSpec("dedupe", {"key": rep.key, "keep": rep.keep})
        scope = "all" if meta.all_records else "view"
        if self.open_bulk is not None:
            return self.open_bulk(spec, scope)
        return self._default_bulk(spec, scope)

    def _default_bulk(self, spec: BulkSpec, scope: str) -> Any:
        from jsonreshape.gui.bulkdialog import BulkDialog

        dialog = BulkDialog.show_for(self.session, parent=self.parentWidget(), scope_default=scope)
        if dialog.task is None:
            dialog.set_spec(spec)
            dialog.set_scope_choice(scope)
            dialog.preview()
        self.bulk_dialog = dialog
        return dialog

    # ================================================================================== lifetime
    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt name
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow() and not self._closing:
            self._update_scope_texts()

    def done(self, result: int) -> None:  # noqa: D401 - Qt name
        """Close: stop the search and the helper tasks, release the result."""
        if not self._closing:
            self._closing = True
            self._scope_timer.stop()
            task = self.task
            if task is not None and getattr(task, "state", "running") == "running":
                task.cancel()
            self.task = None
            for side in self._side_tasks:
                if getattr(side, "state", "running") == "running":
                    side.cancel()
            self._side_tasks = []
            self._req += 1
            lease, self._lease = self._lease, None
            if lease is not None:
                lease.release()
            self.report = None
            try:
                if _OPEN.get(self.session) is self:
                    del _OPEN[self.session]
            except (TypeError, KeyError):  # pragma: no cover
                pass
        super().done(result)
