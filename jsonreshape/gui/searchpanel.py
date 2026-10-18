"""Full-file search panel (Ctrl+Shift+F): docs/ARCHITECTURE.md §11.8, AGENT_PROMPT §6.2.

:class:`SearchPanel` is the content of the search dock. It collects a search
(:class:`~jsonreshape.search.SearchOptions`: text with a history combo, scope keys /
values / both, a path filter validated while typing, mode plain / regex / number, case,
whole word, Latin <-> Cyrillic), runs :func:`jsonreshape.search.search` as a task (kind
``search``, owned by the session: closing the tab cancels it) and shows the hits as they
stream in (``on_hits`` -> ``TaskContext.post``) in a :class:`~jsonreshape.gui.vgrid.VirtualGrid`
with a live summary. The source is the session's current view when it is not the whole
document (a snapshot of the view's record set is taken in the worker, because the session
closes replaced views a few seconds later).

Results are kept per session (:meth:`SearchPanel.set_session` switches the grid to that
tab's results; a new search of the same tab cancels its running one). Activating a hit
selects and reveals its node; the hits can be opened as a record set
(``session.show_records``) or exported to CSV / JSON (every hit, through
:func:`jsonreshape.search.iter_hits`, not only the stored ones). Every search is added to
:class:`~jsonreshape.search.SearchHistory`; picking a history entry restores its options.

A search is refused while the session's filter / sort is still running (the view is not
complete yet). The source label tells what was searched; when the view changed after the
search it says so (and what the next search would cover). The inputs are kept per tab:
switching tabs shows the query of that tab's results. Exporting the hits searches again
(every hit, not only the listed ones); after a cancelled search the user is asked first.
A search export is not owned by the session: reopening the file (F5) or closing its tab
lets it finish (the session closes the old document only after it ended).

Layout: only standard Qt layouts and labels without Python size hints (no Python layout
code runs while Qt lays the dock out).

Threading: the panel never reads the data file. Searches, exports, record-set copies and
the history file run in background tasks; worker results come back through task callbacks.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import search as search_mod
from jsonreshape.errors import Cancelled, JsonReshapeError, UserInputError
from jsonreshape.fileio import normalize_path
from jsonreshape.gui import copyfmt, theme
from jsonreshape.gui.session import NodeRef
from jsonreshape.gui.statusbar import StatusLabel
from jsonreshape.gui.vgrid import Cell, Column, VirtualGrid
from jsonreshape.gui.widgets import confirm, format_count, set_clipboard_text
from jsonreshape.i18n import t
from jsonreshape.paths import parse_pattern
from jsonreshape.recordset import RecordSet, RecordSetBuilder
from jsonreshape.search import SearchHistory, SearchHit, SearchOptions, SearchResult
from jsonreshape.tasks import CancelToken, check

log = logging.getLogger(__name__)

__all__ = ["MAX_HITS", "SearchPanel", "check_not_source", "hit_ref", "snapshot_recordset"]

#: Hits stored (and listed) per search by default; later hits are only counted.
MAX_HITS = 100_000
#: Search texts longer than this are shortened in task titles and labels.
TITLE_CHARS = 40
#: Characters of a selection taken over by :meth:`SearchPanel.focus_search`.
FOCUS_TEXT_MAX = 1000

MODES = ("plain", "regex", "number")
SCOPES = ("keys", "values", "both")      # display order of the scope radios
EXPORT_FORMATS = {".csv": "csv", ".json": "json"}

# single-character stand-ins keep the match spans of a snippet valid
_DISPLAY_MAP = {ord("\n"): "↵", ord("\r"): "↵", ord("\t"): "→"}
_DISPLAY_MAP.update({c: "·" for c in range(0x20) if c not in (9, 10, 13)})
_DISPLAY_MAP.update({c: "\N{REPLACEMENT CHARACTER}" for c in range(0xD800, 0xE000)})


def _display_text(text: str) -> str:
    """``text`` on one line with the same length (spans stay valid)."""
    return text.translate(_DISPLAY_MAP)


def _short(text: str, limit: int = TITLE_CHARS) -> str:
    text = _display_text(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def hit_ref(session: Any, hit: SearchHit) -> NodeRef:
    """The :class:`NodeRef` of a hit (single documents: the path is absolute)."""
    doc = getattr(session, "doc", None)
    if doc is not None and doc.kind == "single":
        return NodeRef(None, tuple(hit.path))
    return NodeRef(int(hit.record), tuple(hit.path))


def snapshot_recordset(rs: RecordSet, cancel: CancelToken | None = None, *, kind: str = "snapshot",
                       description: str = "") -> RecordSet:
    """A copy of ``rs`` as it is now (worker threads only). The session closes a replaced
    view a few seconds after the replacement, so long tasks work on a copy; a set closed
    while it is copied raises ``UserInputError("gui.search.view_changed")``."""
    expected = len(rs)
    builder = RecordSetBuilder(kind, description)
    try:
        try:
            for chunk in rs.iter_chunks():
                check(cancel)
                builder.extend(chunk)
        except Cancelled:
            raise
        except Exception as exc:
            if rs.closed:
                raise UserInputError("gui.search.view_changed") from exc
            raise
        copy = builder.finish()
    except BaseException:
        builder.abort()
        raise
    if rs.closed or len(copy) < expected:
        copy.close()
        raise UserInputError("gui.search.view_changed")
    return copy


class _Lease:
    """A record set shared by the panel (its owner) and background tasks: closed once the
    owner released it and no task uses it any more. Thread-safe."""

    def __init__(self, rs: RecordSet, users: int = 0) -> None:
        self.rs = rs
        self._users = users
        self._released = False
        self._lock = threading.Lock()

    def acquire(self) -> RecordSet:
        with self._lock:
            self._users += 1
        return self.rs

    def done_using(self) -> None:
        with self._lock:
            self._users -= 1
            close = self._released and self._users <= 0
        if close:
            self.rs.close()

    def release(self) -> None:
        with self._lock:
            self._released = True
            close = self._users <= 0
        if close:
            self.rs.close()


@dataclass
class _SourceItem:
    """Posted by a search task: the snapshot of the view it searches."""

    lease: _Lease


class _SearchState:
    """The search results of one session."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.gen = 0
        self.status = "idle"            # idle | running | done | cancelled | failed | stale
        self.task: Any = None
        self.opts: SearchOptions | None = None
        self.hits: list[SearchHit] = []
        self.hit_count = 0
        self.record_count = 0
        self.truncated = False
        self.bytes_scanned = 0
        self.batches = 0
        self.started = 0.0
        self.elapsed = 0.0
        self.result: SearchResult | None = None
        self.records: _Lease | None = None   # result.records
        self.source: _Lease | None = None    # snapshot of the view searched (None: all records)
        self.view_source = False             # the search ran over a view (its snapshot is ``source``)
        self.source_label = ""               # what was searched (the scope text at the start)
        self.view_serial: int | None = None  # session.view_serial at the start
        self.error: BaseException | None = None
        self.message = ""                    # extra status text (export done, file reopened, ...)
        self.inputs: dict[str, Any] | None = None   # the panel's inputs while this tab was shown

    @property
    def running(self) -> bool:
        return self.status == "running"

    def release(self) -> None:
        """Forget the results (record sets released once no task uses them)."""
        for lease in (self.records, self.source):
            if lease is not None:
                lease.release()
        self.records = self.source = None
        if self.result is not None:
            self.result.records = None
        self.result = None


class _HitSource:
    """GridSource over the stored hits of a :class:`_SearchState`."""

    def __init__(self, state: _SearchState | None) -> None:
        self.state = state

    def row_count(self) -> int:
        return len(self.state.hits) if self.state is not None else 0

    def cell(self, row: int, column: Column) -> Cell | None:
        st = self.state
        if st is None or not 0 <= row < len(st.hits):
            return None
        hit = st.hits[row]
        cid = column.id
        if cid == "record":
            text = f"#{hit.record}"
            if hit.key is not None:
                text += " · " + _display_text(hit.key)
            return Cell(text, role="muted")
        if cid == "path":
            return Cell(_display_text(hit.path_text), role="key")
        if cid == "where":
            return Cell(t("search.where.key") if hit.where == "key" else t("search.where.value"), role="muted")
        return Cell(_display_text(hit.text), role="key" if hit.where == "key" else None, spans=tuple(hit.spans))

    def rows_shown(self, first: int, last: int) -> None:
        return None

    def row_label(self, row: int) -> str | None:
        return None


class _KeyFilter(QObject):
    """Enter runs the search, Escape cancels a running one (query and path fields)."""

    def __init__(self, panel: SearchPanel) -> None:
        super().__init__(panel)
        self._panel = panel

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt name)
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._panel.run_search()
                return True
            if key == Qt.Key.Key_Escape and self._panel.is_running():
                self._panel.cancel_search()
                return True
        return False


class SearchPanel(QWidget):
    """The search dock's content (see the module docstring).

    Signals: ``search_started(session)``, ``search_finished(session, status)`` (``done`` |
    ``cancelled`` | ``failed``), ``hits_changed(session)`` (new hits listed),
    ``history_changed()``, ``export_finished(path, count)`` (``count``: a Python int, any
    size), ``records_opened(session)``.
    """

    SOURCE_REFRESH_MS = 300    # live count changes refresh the source label at most this often

    search_started = Signal(object)
    search_finished = Signal(object, str)
    hits_changed = Signal(object)
    history_changed = Signal()
    export_finished = Signal(str, object)
    records_opened = Signal(object)

    def __init__(self, app: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.max_hits: int | None = MAX_HITS
        self._session: Any = None
        self._states: dict[int, _SearchState] = {}
        self._history: list[dict[str, Any]] = []
        self._show_empty_error = False
        self._error_shown = False
        self._busy_error = False        # the error shown is "the view is still being computed"
        self._scope_before_number: str | None = None
        self._source_timer = QTimer(self)
        self._source_timer.setSingleShot(True)
        self._source_timer.setInterval(self.SOURCE_REFRESH_MS)
        self._source_timer.timeout.connect(self._update_source_label)
        self._build()
        self._source = _HitSource(None)
        self.grid.set_source(self._source)
        app.tasks.task_updated.connect(self._on_task_updated)
        app.settings_changed.connect(self._apply_grid_font)
        self._on_mode_changed()
        self._refresh()
        self.reload_history()

    # ================================================================================
    # construction
    # ================================================================================
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        row = QHBoxLayout()
        self.query_combo = QComboBox(self)
        self.query_combo.setEditable(True)
        self.query_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.query_combo.setCompleter(None)   # inline completion would change what Enter searches
        self.query_combo.setMaxVisibleItems(20)
        self.query_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.query_combo.setMinimumContentsLength(8)
        self.query_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.query_combo.setToolTip(t("gui.search.query.tip"))
        self.query_combo.setAccessibleName(t("gui.search.query.name"))
        edit = self.query_combo.lineEdit()
        edit.setClearButtonEnabled(True)
        edit.textChanged.connect(self._on_input_changed)
        self.query_combo.activated.connect(self._history_picked)
        row.addWidget(self.query_combo, 1)
        # one slot: the search button, replaced by the stop button while the search runs
        self.search_button = QPushButton(t("gui.search.run"), self)
        self.search_button.setToolTip(t("gui.search.run.tip"))
        self.search_button.setAutoDefault(False)
        self.search_button.clicked.connect(self.run_search)
        row.addWidget(self.search_button)
        self.cancel_button = QPushButton(t("gui.search.cancel"), self)
        self.cancel_button.setToolTip(t("gui.search.cancel.tip"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.cancel_search)
        self.cancel_button.setVisible(False)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)

        # option rows: plain box layouts (short rows, so a narrow dock still fits them)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        mode_row.addWidget(QLabel(t("gui.search.mode.label"), self))
        self.mode_combo = QComboBox(self)
        for mode in MODES:
            self.mode_combo.addItem(t(f"gui.search.mode.{mode}"), mode)
        self.mode_combo.setToolTip(t("gui.search.mode.tip"))
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)
        options = QHBoxLayout()
        options.setSpacing(8)
        self.case_check = QCheckBox(t("gui.search.case"), self)
        self.case_check.setToolTip(t("gui.search.case.tip"))
        self.word_check = QCheckBox(t("gui.search.word"), self)
        self.word_check.setToolTip(t("gui.search.word.tip"))
        self.translit_check = QCheckBox(t("gui.search.translit"), self)
        self.translit_check.setToolTip(t("gui.search.translit.tip"))
        for box in (self.case_check, self.word_check, self.translit_check):
            box.toggled.connect(self._on_input_changed)
            options.addWidget(box)
        options.addStretch(1)
        layout.addLayout(options)

        scopes = QHBoxLayout()
        scopes.setSpacing(8)
        scopes.addWidget(QLabel(t("gui.search.scope.label"), self))
        self.scope_group = QButtonGroup(self)
        self.scope_buttons: dict[str, QRadioButton] = {}
        for i, scope in enumerate(SCOPES):
            button = QRadioButton(t(f"gui.search.scope.{scope}"), self)
            self.scope_group.addButton(button, i)
            self.scope_buttons[scope] = button
            scopes.addWidget(button)
        self.scope_buttons["both"].setChecked(True)
        self.scope_group.idToggled.connect(self._on_scope_toggled)
        scopes.addStretch(1)
        layout.addLayout(scopes)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel(t("gui.search.path.label"), self))
        self.path_edit = QLineEdit(self)
        self.path_edit.setPlaceholderText(t("gui.search.path.placeholder"))
        self.path_edit.setToolTip(t("gui.search.path.tip"))
        self.path_edit.setClearButtonEnabled(True)
        self.path_edit.textChanged.connect(self._on_input_changed)
        path_row.addWidget(self.path_edit, 1)
        layout.addLayout(path_row)

        self._key_filter = _KeyFilter(self)
        edit.installEventFilter(self._key_filter)
        self.path_edit.installEventFilter(self._key_filter)

        self.error_label = QLabel(self)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        self.source_label = StatusLabel(self, mode=Qt.TextElideMode.ElideRight, fixed=False)
        self.source_label.setContentsMargins(0, 0, 0, 0)
        self.source_label.update_metrics()
        layout.addWidget(self.source_label)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(6)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        self.summary_label = QLabel(self)
        self.summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary_label)
        self.note_label = QLabel(self)
        self.note_label.setTextFormat(Qt.TextFormat.PlainText)
        self.note_label.setWordWrap(True)
        self.note_label.setVisible(False)
        layout.addWidget(self.note_label)

        self.grid = VirtualGrid(self)
        self.grid.set_selection_mode("rows")
        self.grid.set_columns([
            Column("record", t("gui.search.col.record"), width=96),
            Column("path", t("gui.search.col.path"), width=200),
            Column("where", t("gui.search.col.where"), width=70),
            Column("text", t("gui.search.col.text"), width=420),
        ])
        self.grid.set_placeholder_text(t("gui.search.empty"))
        self.grid.activated.connect(lambda row, _col: self.activate_hit(int(row)))
        self.grid.context_menu_requested.connect(self._context_menu)
        self.grid.set_key_handler(self._grid_key)
        self._apply_grid_font()
        layout.addWidget(self.grid, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self.open_button = QPushButton(t("gui.search.open_records"), self)
        self.open_button.setToolTip(t("gui.search.open_records.tip"))
        self.open_button.setAutoDefault(False)
        self.open_button.clicked.connect(self.open_as_records)
        bottom.addWidget(self.open_button)
        self.export_button = QPushButton(t("gui.search.export"), self)
        self.export_button.setToolTip(t("gui.search.export.tip"))
        self.export_button.setAutoDefault(False)
        self.export_button.clicked.connect(self.choose_export_file)
        bottom.addWidget(self.export_button)
        bottom.addStretch(1)
        layout.addLayout(bottom)
        self._style_labels()
        theme.notifier().theme_changed.connect(self._style_labels)

    def _apply_grid_font(self) -> None:
        state = getattr(self.app, "state", None)
        family = size = None
        if state is not None:
            family = state.get("mono_font") or None
            size = state.get("mono_size") or None
        try:
            self.grid.set_font(theme.mono_font(family, size))
        except Exception:  # pragma: no cover - font problems never break the panel
            log.debug("could not set the result font", exc_info=True)

    def _style_labels(self) -> None:
        danger = theme.color("danger").name()
        muted = theme.color("muted").name()
        self.error_label.setStyleSheet(f"color: {danger};")
        self.source_label.setStyleSheet(f"color: {muted};")
        self.note_label.setStyleSheet(f"color: {theme.color('warn').name()};")

    # ================================================================================
    # sessions
    # ================================================================================
    def session(self) -> Any:
        return self._session

    def set_session(self, session: Any) -> None:
        """Show the search results of ``session`` (``None``: no document). The inputs are
        kept per session: the ones shown with the previous session are remembered, the
        new session's (its last inputs, else the query of its results) are restored."""
        if session is not None and getattr(session, "closed", False):
            session = None
        old = self._session
        if old is not session and old is not None:
            old_st = self._state_for(old, create=False)
            if old_st is not None:
                old_st.inputs = self.input_values()
        self._session = session
        st = self._state_for(session) if session is not None else None
        if old is not session and st is not None:
            saved = st.inputs
            if saved is None and st.opts is not None and st.status != "stale":
                saved = st.opts.to_dict()
            if saved is not None:
                self.set_options(saved)
        self._source.state = st
        if self._busy_error and old is not session:
            self._busy_error = False
            self._validate()          # the "view busy" error was about the other document
        self.grid.reset()
        self._refresh()

    def _state_for(self, session: Any, create: bool = True) -> _SearchState | None:
        st = self._states.get(id(session))
        if st is not None and st.session is not session:   # a recycled id
            st.release()
            st = None
            del self._states[id(session)]
        if st is None and create:
            st = _SearchState(session)
            self._states[id(session)] = st
            session.state_changed.connect(self._on_session_state)
            session.view_changed.connect(self._on_session_view)
            session.count_changed.connect(self._on_session_count)
            busy_changed = getattr(session, "view_busy_changed", None)
            if busy_changed is not None:
                busy_changed.connect(self._on_session_busy)
        return st

    def state(self, session: Any = None) -> _SearchState | None:
        """The result state of ``session`` (default: the current one); for tests/callers."""
        session = session if session is not None else self._session
        if session is None:
            return None
        return self._state_for(session, create=False)

    def _on_session_state(self, value: str) -> None:
        session = self.sender()
        if session is None:
            return
        st = self._states.get(id(session))
        if st is None or st.session is not session:
            return
        if value == "closed":
            st.gen += 1
            st.release()
            del self._states[id(session)]
            if session is self._session:
                self.set_session(None)
            return
        if value == "opening" and (st.opts is not None or st.hits or st.running or st.result is not None):
            # reopened: the tasks were detached and the record numbers may have changed (also a
            # cancelled search without hits: exporting it would search the new document)
            st.gen += 1
            st.release()
            st.hits = []
            st.hit_count = st.record_count = 0
            st.truncated = False
            st.task = None
            st.opts = None
            st.view_source = False
            st.status = "stale"
            st.message = t("gui.search.stale")
            if session is self._session:
                self.grid.reset()
                self._refresh()

    def _on_session_busy(self, busy: bool) -> None:
        """The view's filter / sort finished: the "view busy" error is stale now."""
        if not busy and self._busy_error and self.sender() is self._session and self._session is not None:
            self._busy_error = False
            self._validate()

    def _on_session_view(self) -> None:
        if self.sender() is self._session and self._session is not None:
            self._source_timer.stop()
            self._update_source_label()
            self._refresh_buttons()

    def _on_session_count(self) -> None:
        """Live counts (indexing, a growing filter result): the label at most every
        :attr:`SOURCE_REFRESH_MS`."""
        if self.sender() is self._session and self._session is not None and not self._source_timer.isActive():
            self._source_timer.start()

    # ================================================================================
    # inputs
    # ================================================================================
    def mode(self) -> str:
        return str(self.mode_combo.currentData() or "plain")

    def scope(self) -> str:
        for scope, button in self.scope_buttons.items():
            if button.isChecked():
                return scope
        return "both"

    def query_text(self) -> str:
        return self.query_combo.currentText()

    def set_query_text(self, text: str) -> None:
        self.query_combo.setEditText(text)

    def _flags(self) -> dict[str, bool]:
        """The case / whole word / translit options that apply in the current mode (a
        disabled check box counts as off)."""
        mode = self.mode()
        return {
            "case_sensitive": self.case_check.isChecked() and mode != "number",
            "whole_word": self.word_check.isChecked() and mode != "number",
            "translit": self.translit_check.isChecked() and mode == "plain",
        }

    def options(self) -> SearchOptions:
        """The search described by the inputs (``UserInputError`` / ``QuerySyntaxError`` /
        ``PathSyntaxError`` when invalid)."""
        path = self.path_edit.text().strip() or None
        return SearchOptions.build(self.query_text(), mode=self.mode(), scope=self.scope(), path=path, **self._flags())

    def input_values(self) -> dict[str, Any]:
        """The inputs as a :meth:`set_options` dict (check boxes as shown)."""
        return {
            "text": self.query_text(), "mode": self.mode(), "case_sensitive": self.case_check.isChecked(),
            "whole_word": self.word_check.isChecked(), "translit": self.translit_check.isChecked(),
            "scope": self._scope_before_number or self.scope(), "path": self.path_edit.text(),
        }

    def set_options(self, data: dict[str, Any]) -> None:
        """Fill the inputs from a :meth:`SearchOptions.to_dict` dict (missing keys keep
        their values; ``text`` included when present)."""
        if not isinstance(data, dict):
            return
        if isinstance(data.get("text"), str):
            self.set_query_text(data["text"])
        mode = data.get("mode")
        if mode in MODES:
            self.mode_combo.setCurrentIndex(MODES.index(mode))
        for key, box in (("case_sensitive", self.case_check), ("whole_word", self.word_check),
                         ("translit", self.translit_check)):
            if isinstance(data.get(key), bool):
                box.setChecked(data[key])
        scope = data.get("scope")
        if scope in self.scope_buttons:
            self._scope_before_number = None
            self.scope_buttons[scope].setChecked(True)
            self._apply_scope_rules()
        if "path" in data:
            path = data.get("path")
            self.path_edit.setText(path if isinstance(path, str) else "")
        self._validate()

    def focus_search(self, text: str | None = None) -> None:
        """Focus the search field (Ctrl+Shift+F), optionally with ``text`` (e.g. the
        selected text: its first line) selected."""
        if text:
            first = str(text).strip().splitlines()[0] if str(text).strip() else ""
            if first:
                self.set_query_text(first[:FOCUS_TEXT_MAX])
        edit = self.query_combo.lineEdit()
        edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        edit.selectAll()

    def _on_mode_changed(self, _index: int = 0) -> None:
        mode = self.mode()
        plain = mode == "plain"
        number = mode == "number"
        self.case_check.setEnabled(not number)
        self.word_check.setEnabled(not number)
        self.translit_check.setEnabled(plain)
        self.case_check.setToolTip(t("gui.search.number_ignores") if number else t("gui.search.case.tip"))
        self.word_check.setToolTip(t("gui.search.number_ignores") if number else t("gui.search.word.tip"))
        self.translit_check.setToolTip(t("gui.search.translit.tip") if plain else t("gui.search.translit.plain_only"))
        self.query_combo.lineEdit().setPlaceholderText(t(f"gui.search.query.placeholder.{mode}"))
        self._apply_scope_rules()
        self._on_input_changed()

    def _apply_scope_rules(self) -> None:
        """The number mode compares values only: "keys" is disabled there (a chosen
        "keys" becomes "values" and comes back when the mode changes again)."""
        keys = self.scope_buttons["keys"]
        if self.mode() == "number":
            if keys.isChecked():
                self._scope_before_number = "keys"
                self.scope_buttons["values"].setChecked(True)
            keys.setEnabled(False)
            keys.setToolTip(t("gui.search.scope.keys_number"))
        else:
            keys.setEnabled(True)
            keys.setToolTip("")
            if self._scope_before_number == "keys":
                self._scope_before_number = None
                keys.setChecked(True)

    def _on_scope_toggled(self, _id: int, checked: bool) -> None:
        if checked and self.mode() != "number":
            self._scope_before_number = None
        self._on_input_changed()

    def _on_input_changed(self, *_args: Any) -> None:
        self._validate()

    def _validate(self) -> bool:
        """Check the inputs; show the error inline. True when a search can run."""
        path_error = None
        path = self.path_edit.text().strip()
        if path:
            try:
                parse_pattern(path)
            except JsonReshapeError as exc:
                path_error = exc
        self._mark(self.path_edit, path_error is not None)
        query_error = None
        if self.query_text() or self._show_empty_error:
            try:
                SearchOptions.build(self.query_text() or "", mode=self.mode(), scope=self.scope(), **self._flags())
            except JsonReshapeError as exc:
                query_error = exc
        self._mark(self.query_combo.lineEdit(), query_error is not None)
        errors = [e for e in (query_error, path_error) if e is not None]
        if errors:
            self._set_error("\n".join(_error_text(e) for e in errors))
        else:
            self._set_error(None)
        return not errors and bool(self.query_text())

    def _mark(self, widget: QWidget, bad: bool) -> None:
        if bad:
            widget.setStyleSheet(f"border: 1px solid {theme.color('danger').name()};")
        else:
            widget.setStyleSheet("")
        widget.setProperty("invalid", bad)

    def _set_error(self, text: str | None) -> None:
        """Show (``None``: hide) the inline error; while one is shown it replaces the
        summary of the previous search."""
        self.error_label.setText(copyfmt.escape_surrogates(text or ""))
        self.error_label.setVisible(bool(text))
        self._error_shown = bool(text)
        self._busy_error = False
        self._sync_summary_visibility()

    def _sync_summary_visibility(self) -> None:
        self.summary_label.setVisible(not self._error_shown)
        self.note_label.setVisible(not self._error_shown and bool(self.note_label.text()))

    def error_text(self) -> str:
        """The inline error shown (empty when none)."""
        return self.error_label.text() if not self.error_label.isHidden() else ""

    # ================================================================================
    # history
    # ================================================================================
    def reload_history(self) -> Any:
        """Read the search history in a silent task, then fill the combo."""
        def work(_ctx: Any) -> list[dict[str, Any]]:
            return SearchHistory().list()

        return self.app.tasks.run(work, title=t("gui.search.task.history"), kind="history", owner=self, silent=True,
                                  on_done=self._history_loaded, on_error=self._history_failed)

    def _history_loaded(self, entries: list[dict[str, Any]]) -> None:
        self._history = list(entries)
        current = self.query_text()
        combo = self.query_combo
        combo.blockSignals(True)
        try:
            combo.clear()
            for entry in self._history:
                combo.addItem(_display_text(entry["text"]))
                combo.setItemData(combo.count() - 1, _history_tip(entry), Qt.ItemDataRole.ToolTipRole)
            combo.setCurrentIndex(-1)
            combo.setEditText(current)
        finally:
            combo.blockSignals(False)
        self.history_changed.emit()

    def _history_failed(self, exc: BaseException) -> None:
        log.warning("could not read the search history: %s", exc)

    def history(self) -> list[dict[str, Any]]:
        """The history entries shown in the combo (most recent first)."""
        return list(self._history)

    def _history_picked(self, index: int) -> None:
        if 0 <= index < len(self._history):
            entry = self._history[index]
            options = dict(entry.get("options") or {})
            options["text"] = entry["text"]
            self.set_options(options)

    def _history_add(self, opts: SearchOptions) -> None:
        text = opts.query.text
        data = opts.to_dict()

        def work(_ctx: Any) -> list[dict[str, Any]]:
            history = SearchHistory()
            history.add(text, data)
            return history.list()

        self.app.tasks.run(work, title=t("gui.search.task.history"), kind="history", owner=self, silent=True,
                           on_done=self._history_loaded, on_error=self._history_failed)

    # ================================================================================
    # searching
    # ================================================================================
    def is_running(self, session: Any = None) -> bool:
        st = self.state(session)
        return st is not None and st.running

    def run_search(self) -> Any:
        """Start a search of the current session with the inputs (a running search of the
        same session is cancelled). Returns the task, or ``None`` when the inputs are
        invalid (the error is shown inline)."""
        self._show_empty_error = True
        try:
            valid = self._validate()
        finally:
            self._show_empty_error = False
        session = self._session
        if not valid:
            if not self.error_text():
                self._set_error(UserInputError("textmatch.empty_query").message)
            return None
        if session is None or getattr(session, "closed", False):
            self._set_error(t("gui.search.no_document"))
            return None
        doc = session.doc
        if doc is None:
            self._set_error(UserInputError("gui.session.not_open", name=session.name).message)
            return None
        if getattr(session, "view_busy", False):
            # the filter / sort result is still growing: searching it now would miss records
            self._state_for(session)             # its view_busy_changed clears the message
            self._set_error(t("gui.search.view_busy"))
            self._busy_error = True
            return None
        try:
            opts = self.options()
        except (JsonReshapeError, ValueError, TypeError) as exc:
            self._set_error(_error_text(exc))
            return None
        st = self._state_for(session)
        self._stop(st)
        st.release()
        st.gen += 1
        gen = st.gen
        st.status = "running"
        st.opts = opts
        st.hits = []
        st.hit_count = st.record_count = st.bytes_scanned = st.batches = 0
        st.truncated = False
        st.error = None
        st.message = ""
        st.started = time.monotonic()
        st.elapsed = 0.0
        view = None if session.view_is_base else session.view_recordset()
        st.view_source = view is not None
        st.source_label = self._scope_text(session)
        st.view_serial = getattr(session, "view_serial", None)
        max_hits = self.max_hits

        def work(ctx: Any) -> SearchResult:
            src = None
            lease = None
            if view is not None:
                ctx.progress.set_message(t("gui.search.progress.snapshot"))
                snap = snapshot_recordset(view, ctx.cancel, kind="search-source")
                lease = _Lease(snap, users=1)
                ctx.post(_SourceItem(lease))
                src = snap
                ctx.progress.set_message("")
            try:
                return search_mod.search(doc, opts, src, max_hits=max_hits, progress=ctx.progress, cancel=ctx.cancel,
                                         on_hits=ctx.post)
            finally:
                if lease is not None:
                    lease.done_using()

        st.task = self.app.tasks.run(
            work, title=t("gui.search.task", text=_short(opts.query.text), name=session.name), kind="search",
            owner=session,
            on_item=lambda item: self._on_item(st, gen, item),
            on_done=lambda result: self._on_done(st, gen, result),
            on_error=lambda exc: self._on_failed(st, gen, exc),
            on_cancel=lambda: self._on_cancelled(st, gen))
        self._history_add(opts)
        if st.session is self._session:
            self.grid.reset()
            self._refresh()
        self.search_started.emit(session)
        return st.task

    def cancel_search(self) -> None:
        """Stop the current session's search (the hits found so far stay)."""
        st = self.state()
        if st is not None and st.running and st.task is not None:
            st.task.cancel()

    def _stop(self, st: _SearchState) -> None:
        if st.running and st.task is not None:
            st.task.cancel()
        st.task = None

    def _on_item(self, st: _SearchState, gen: int, item: Any) -> None:
        if gen != st.gen:
            if isinstance(item, _SourceItem):
                item.lease.release()
            return
        if isinstance(item, _SourceItem):
            if st.source is not None:
                st.source.release()
            st.source = item.lease
            return
        if not isinstance(item, list):
            return
        st.batches += 1
        if item:
            st.hits.extend(item)
        st.hit_count = int(getattr(item, "hit_count", st.hit_count))
        st.record_count = int(getattr(item, "record_count", st.record_count))
        st.truncated = bool(getattr(item, "truncated", st.truncated))
        st.bytes_scanned = int(getattr(item, "bytes_scanned", st.bytes_scanned))
        st.elapsed = time.monotonic() - st.started
        if st.session is self._session:
            self.grid.rows_changed()
            self._refresh()
        self.hits_changed.emit(st.session)

    def _on_done(self, st: _SearchState, gen: int, result: SearchResult) -> None:
        if gen != st.gen:
            closer = getattr(result, "close", None)
            if closer is not None:
                closer()
            return
        st.task = None
        st.status = "done"
        st.result = result
        records = result.records
        st.records = _Lease(records) if isinstance(records, RecordSet) else None
        if st.hits is not result.hits:
            st.hits = result.hits
        st.hit_count = result.hit_count
        st.record_count = result.record_count
        st.truncated = result.truncated
        st.bytes_scanned = result.bytes_scanned
        st.elapsed = result.elapsed
        self._finished(st, "done")

    def _on_cancelled(self, st: _SearchState, gen: int) -> None:
        if gen != st.gen:
            return
        st.task = None
        st.status = "cancelled"
        st.elapsed = time.monotonic() - st.started
        self._finished(st, "cancelled")

    def _on_failed(self, st: _SearchState, gen: int, exc: BaseException) -> None:
        if gen != st.gen:
            return
        st.task = None
        st.status = "failed"
        st.error = exc
        st.elapsed = time.monotonic() - st.started
        self._finished(st, "failed")
        if not isinstance(exc, Cancelled):
            self.app.report_error(exc, self)

    def _finished(self, st: _SearchState, status: str) -> None:
        if st.session is self._session:
            self.grid.rows_changed()
            self._refresh()
        self.search_finished.emit(st.session, status)

    def _on_task_updated(self, task: Any) -> None:
        st = self.state()
        if st is None or st.task is not task:
            return
        info = task.info()
        if info is not None and info.unit == "bytes":
            # hit batches stop while nothing matches: keep the speed current from the progress
            st.bytes_scanned = max(st.bytes_scanned, int(info.done))
        self._update_progress(task)
        if st.running:
            self.summary_label.setText(self._summary(st))

    def _update_progress(self, task: Any) -> None:
        info = task.info() if task is not None else None
        if info is None or info.fraction is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(max(0.0, min(1.0, info.fraction)) * 1000))

    # ================================================================================
    # display
    # ================================================================================
    def _scope_text(self, session: Any) -> str:
        """What a search of ``session`` would cover now ("barcha yozuvlar (300 ta)")."""
        if session is None or session.doc is None:
            return ""
        count = format_count(session.view_len())
        if session.view_is_base:
            return t("gui.search.domain.all", count=count)
        return t("gui.search.domain.view", label=session.view_label(), count=count)

    def _source_text(self, session: Any) -> str:
        """The source label: what the search covers -- and, when the view changed after
        the last search, what that search covered and what the next one would."""
        live = self._scope_text(session)
        if not live:
            return ""
        st = self._states.get(id(session))
        if st is not None and st.session is session and st.opts is not None \
                and st.status in ("running", "done", "cancelled", "failed") and st.source_label \
                and st.view_serial is not None and st.view_serial != getattr(session, "view_serial", None):
            return t("gui.search.source.changed", searched=st.source_label, live=live)
        return t("gui.search.source", scope=live)

    def _update_source_label(self) -> None:
        self.source_label.setText(self._source_text(self._session))

    def summary_text(self) -> str:
        return self.summary_label.text()

    def _refresh(self) -> None:
        st = self.state()
        session = self._session
        has_doc = session is not None and session.doc is not None
        running = st is not None and st.running
        self.search_button.setEnabled(session is not None)
        self.search_button.setVisible(not running)
        self.cancel_button.setEnabled(running)
        self.cancel_button.setVisible(running)
        self.progress_bar.setVisible(running)
        if running:
            self._update_progress(st.task)
        self._update_source_label()
        summary = ""
        note = ""
        if st is not None:
            summary = self._summary(st)
            if st.truncated and st.status != "done" and st.hits:
                note = t("gui.search.truncated_note", shown=format_count(len(st.hits)))
        elif session is None:
            summary = t("gui.search.no_document")
        self.summary_label.setText(copyfmt.escape_surrogates(summary))
        self.note_label.setText(note)
        self._sync_summary_visibility()
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        st = self.state()
        session = self._session
        has_doc = session is not None and session.doc is not None
        can_open, why = self._can_open(st)
        self.open_button.setEnabled(has_doc and can_open)
        self.open_button.setToolTip(t("gui.search.open_records.tip") if can_open else why)
        can_export, why_not = self._can_export(st)
        self.export_button.setEnabled(has_doc and can_export)
        if can_export:
            tip = t("gui.search.export.rerun_tip") if st is not None and st.status == "cancelled" \
                else t("gui.search.export.tip")
        else:
            tip = why_not
        self.export_button.setToolTip(tip)

    def _summary(self, st: _SearchState) -> str:
        if st.status == "running":
            elapsed = max(1e-6, time.monotonic() - st.started)
            rate = st.bytes_scanned / 1e6 / elapsed
            return t("gui.search.live", hits=format_count(st.hit_count), records=format_count(st.record_count),
                     rate=f"{rate:.0f}")
        if st.status == "done" and st.result is not None:
            text = st.result.summary()
        elif st.status == "cancelled":
            text = t("gui.search.cancelled", hits=format_count(st.hit_count), records=format_count(st.record_count))
        elif st.status == "failed":
            msg = _error_text(st.error) if st.error is not None else ""
            text = t("gui.search.failed", error=msg)
        else:
            text = ""
        if st.message:
            text = (text + "\n" + st.message).strip()
        return text

    def _can_export(self, st: _SearchState | None) -> tuple[bool, str]:
        if st is None or st.opts is None or st.status not in ("done", "cancelled", "running"):
            return False, t("gui.search.export.need_search")
        if st.status == "running":
            return False, t("gui.search.open_records.running")
        if st.view_source and st.source is None:   # stopped before the view was copied
            return False, t("gui.search.export.no_source")
        return True, ""

    def _can_open(self, st: _SearchState | None) -> tuple[bool, str]:
        if st is None or st.opts is None:
            return False, t("gui.search.open_records.need_search")
        doc = getattr(st.session, "doc", None)
        if doc is not None and doc.kind == "single":
            return False, t("gui.search.open_records.single")
        if st.status == "running":
            return False, t("gui.search.open_records.running")
        if st.status == "done" and st.records is not None:
            return True, ""
        if st.status == "cancelled":
            if st.truncated:
                return False, t("gui.search.open_records.truncated")
            return bool(st.hits), t("gui.search.open_records.no_hits")
        return False, t("gui.search.open_records.need_search")

    # ================================================================================
    # hits
    # ================================================================================
    def hit_count_shown(self) -> int:
        """Rows in the results grid."""
        return self._source.row_count()

    def hit_at(self, row: int) -> SearchHit | None:
        st = self._source.state
        if st is None or not 0 <= row < len(st.hits):
            return None
        return st.hits[row]

    def activate_hit(self, row: int) -> NodeRef | None:
        """Select the hit's node in the session and reveal it in the tree."""
        hit = self.hit_at(row)
        session = self._session
        if hit is None or session is None or getattr(session, "closed", False):
            return None
        ref = hit_ref(session, hit)
        session.select(ref, reveal=True)
        return ref

    def selected_rows(self) -> list[int]:
        """Selected result rows (the current row when nothing is selected), ascending."""
        rows: set[int] = set()
        n = self.hit_count_shown()
        for r0, r1, _c0, _c1 in self.grid.selected_ranges():
            rows.update(range(max(0, r0), min(r1, n - 1) + 1))
        if not rows:
            cur = self.grid.current()[0]
            if 0 <= cur < n:
                rows.add(cur)
        return sorted(rows)

    def copy_rows(self, rows: list[int] | None = None) -> str:
        """Copy hits as tab-separated lines (record, key, path, where, text)."""
        rows = self.selected_rows() if rows is None else rows
        lines = []
        for row in rows:
            hit = self.hit_at(row)
            if hit is None:
                continue
            where = t("search.where.key") if hit.where == "key" else t("search.where.value")
            lines.append([str(hit.record), hit.key or "", hit.path_text, where, hit.text])
        if not lines:
            return ""
        return set_clipboard_text(copyfmt.tsv_text(lines))

    def copy_path(self, row: int) -> str | None:
        hit = self.hit_at(row)
        session = self._session
        if hit is None or session is None:
            return None
        try:
            path = session.absolute_path(hit_ref(session, hit))
        except JsonReshapeError:
            path = tuple(hit.path)
        return set_clipboard_text(copyfmt.path_text(path, "dotted"))

    def copy_text(self, row: int) -> str | None:
        hit = self.hit_at(row)
        return set_clipboard_text(hit.text) if hit is not None else None

    def copy_record(self, row: int) -> str | None:
        hit = self.hit_at(row)
        return set_clipboard_text(f"#{hit.record}") if hit is not None else None

    def hit_menu(self, row: int) -> QMenu:
        """The context menu of result row ``row`` (actions have object names:
        ``show_in_tree``, ``copy_path``, ``copy_text``, ``copy_record``, ``copy_rows``)."""
        menu = QMenu(self)
        hit = self.hit_at(row)
        items: list[tuple[str, str, Callable[[], Any], bool]] = [
            ("show_in_tree", t("gui.search.menu.show"), lambda: self.activate_hit(row), hit is not None),
            ("copy_path", t("gui.search.menu.copy_path"), lambda: self.copy_path(row), hit is not None),
            ("copy_text", t("gui.search.menu.copy_text"), lambda: self.copy_text(row), hit is not None),
            ("copy_record", t("gui.search.menu.copy_record"), lambda: self.copy_record(row), hit is not None),
            ("copy_rows", t("gui.search.menu.copy_rows"), lambda: self.copy_rows(), self.hit_count_shown() > 0),
        ]
        for name, text, callback, enabled in items:
            if name == "copy_path":
                menu.addSeparator()
            action = QAction(text, menu)
            action.setObjectName(name)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _checked=False, cb=callback: cb())
            if name == "copy_rows":
                menu.addSeparator()
                action.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))
            menu.addAction(action)
        return menu

    def _context_menu(self, row: Any, _col: int, pos: QPoint) -> None:
        row = int(row)
        if row < 0:
            return
        if not self.grid.is_selected(row):
            self.grid.set_current(row)
        menu = self.hit_menu(row)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.popup(pos)

    def _grid_key(self, event: QKeyEvent) -> bool:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_rows()
            return True
        return False

    # ================================================================================
    # open as a record set
    # ================================================================================
    def open_as_records(self) -> Any:
        """Show the records with hits as the session's view (``session.show_records``;
        the record set is copied in a silent task). Returns the task (``None`` when not
        possible)."""
        st = self.state()
        session = self._session
        ok, why = self._can_open(st)
        if not ok or st is None or session is None or st.opts is None:
            if why:
                self._set_error(why)
            return None
        label = t("gui.search.records_label", text=_short(st.opts.query.text))
        lease = st.records if st.status == "done" else None
        indices: list[int] | None = None
        if lease is None:
            indices = []
            last = -1
            for hit in st.hits:
                if hit.record != last:
                    indices.append(int(hit.record))
                    last = hit.record
        src = lease.acquire() if lease is not None else None

        def work(ctx: Any) -> RecordSet:
            try:
                if src is not None:
                    return snapshot_recordset(src, ctx.cancel, kind="search", description=label)
                return RecordSet.from_indices(indices or [], kind="search", description=label)
            finally:
                if lease is not None:
                    lease.done_using()

        def done(rs: RecordSet) -> None:
            if getattr(session, "closed", False):
                rs.close()
                return
            session.show_records(rs, label)
            self.records_opened.emit(session)

        return self.app.tasks.run(work, title=t("gui.search.task.open"), kind="records", owner=session, silent=True,
                                  on_done=done, on_error=lambda exc: self.app.report_error(exc, self))

    # ================================================================================
    # export
    # ================================================================================
    def choose_export_file(self) -> None:
        """Ask for the output file (CSV / JSON), then :meth:`export_hits_to`."""
        st = self.state()
        session = self._session
        if st is None or st.opts is None or session is None or not self._can_export(st)[0]:
            return
        start = self.app.state.get("search_export_dir") or os.path.dirname(session.path)
        base = os.path.splitext(session.name)[0] + "-search.csv"
        path, selected = QFileDialog.getSaveFileName(self, t("gui.search.export.title"), os.path.join(start, base),
                                                     t("gui.search.export.filter"))
        if not path:
            return
        if st.status == "cancelled" and not self._confirm(t("gui.search.export.rerun_confirm")):
            return
        fmt = EXPORT_FORMATS.get(os.path.splitext(path)[1].lower())
        if fmt is None:
            fmt = "json" if "json" in (selected or "").lower() else "csv"
            path += "." + fmt
        try:
            self.app.state.set("search_export_dir", os.path.dirname(path))
        except (TypeError, ValueError):
            pass
        self.export_hits_to(path, fmt)

    def _confirm(self, text: str) -> bool:
        """A yes/no question (tests replace this method)."""
        return confirm(self, text)

    def export_hits_to(self, path: str, fmt: str | None = None) -> Any:
        """Export EVERY hit of the last search (the same options and source, searched
        again through :func:`jsonreshape.search.iter_hits`) to ``path`` (``fmt``: ``csv`` |
        ``json``; default from the extension). Returns the task (``None`` on refusal).

        The task belongs to the panel, not to the session: reopening the file or closing
        its tab does not stop it (the session keeps the old document open until it ended);
        the outcome is shown in the panel and the status bar."""
        st = self.state()
        session = self._session
        ok, why = self._can_export(st)
        if not ok or st is None or st.opts is None or session is None or session.doc is None:
            self._set_error(why or t("gui.search.export.need_search"))
            return None
        path = os.path.abspath(os.fspath(path))
        kind = fmt or EXPORT_FORMATS.get(os.path.splitext(path)[1].lower())
        try:
            if kind not in ("csv", "json"):
                raise UserInputError("search.export.bad_format", fmt=os.path.splitext(path)[1] or str(fmt))
            doc = session.doc
            check_not_source(path, (doc.source_path, doc.data_path, session.path))
        except UserInputError as exc:
            self._set_error(_error_text(exc))
            return None
        self._set_error(None)
        opts = st.opts
        lease = st.source
        src = lease.acquire() if lease is not None else None
        name = os.path.basename(path)

        def work(ctx: Any) -> int:
            try:
                ctx.progress.set_message(t("search.progress.export", name=name))

                def hits() -> Any:
                    yield from search_mod.iter_hits(doc, opts, src, progress=ctx.progress, cancel=ctx.cancel)
                    if src is not None and src.closed:   # pragma: no cover - leases prevent it
                        raise UserInputError("gui.search.view_changed")

                return search_mod.export_hits(hits(), path, kind, cancel=ctx.cancel)
            finally:
                if lease is not None:
                    lease.done_using()

        def done(count: int) -> None:
            message = t("gui.search.export.done", count=format_count(count), path=path)
            st.message = message
            if st.session is self._session:
                self._refresh()
            show_status = getattr(self.app, "show_status", None)
            if callable(show_status):
                show_status(message, 8000)
            self.export_finished.emit(path, int(count))

        task = self.app.tasks.run(work, title=t("gui.search.task.export", name=name), kind="export", owner=self,
                                  on_done=done, on_error=lambda exc: self.app.report_error(exc, self))
        keep = getattr(session, "keep_document_for", None)
        if callable(keep):
            keep(task)            # the document stays open until the export ended
        return task

    # ================================================================================
    # lifetime
    # ================================================================================
    def shutdown(self) -> None:
        """Cancel every running search and release the results (main window closing)."""
        self.app.tasks.detach(self)
        for st in list(self._states.values()):
            self._stop(st)
            st.gen += 1
            st.release()
        self._states.clear()
        self._session = None
        self._source.state = None


def check_not_source(path: str | os.PathLike[str], sources: Any) -> None:
    """``UserInputError("writer.error.same_file")`` when ``path`` is (or aliases: same file
    through another name, a junction, a share; ``os.path.samefile``) one of ``sources``."""
    from jsonreshape.fileio import long_path

    target = normalize_path(path)
    for src in sources:
        if src is None:
            continue
        if target == normalize_path(src):
            raise UserInputError("writer.error.same_file", path=str(path))
        try:
            same = os.path.samefile(long_path(path), long_path(src))
        except (OSError, ValueError):
            same = False
        if same:
            raise UserInputError("writer.error.same_file", path=str(path))


def _error_text(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    if isinstance(exc, JsonReshapeError):
        return exc.message
    return str(exc) or type(exc).__name__


def _history_tip(entry: dict[str, Any]) -> str:
    options = entry.get("options") or {}
    mode = options.get("mode") if options.get("mode") in MODES else "plain"
    scope = options.get("scope") if options.get("scope") in ("both", "keys", "values") else "both"
    parts = [t(f"gui.search.mode.{mode}"), t(f"search.scope.{scope}")]
    if options.get("case_sensitive"):
        parts.append(t("gui.search.case"))
    if options.get("whole_word"):
        parts.append(t("gui.search.word"))
    if options.get("translit"):
        parts.append(t("gui.search.translit"))
    if options.get("path"):
        parts.append(t("gui.search.path.tip_value", path=str(options["path"])))
    return " · ".join(parts)
