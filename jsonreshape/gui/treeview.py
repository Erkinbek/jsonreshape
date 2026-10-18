"""TreeView: the central tree of a document (docs/ARCHITECTURE.md §11.7 "Tree", AGENT_PROMPT §4.1).

Rows are the nodes of a lazily expanded tree flattened by a :class:`~jsonreshape.gui.flattree.FlatTree`
and painted by a :class:`~jsonreshape.gui.vgrid.VirtualGrid` (no per-row state, 10**8 rows fine).

* **Top level** = the records of the session's *current view* (filtered / sorted / search
  results included), key path ``(record,)``. Wrapped documents get a first row for the
  wrapper object (``("wrapper",)``; its record-array member shows the record count and is not
  expanded inline: the records are the rows below). Single documents have one root row
  ``$`` (``("root",)``), expanded on open.
* **Columns**: Kalit (indent guides, expander, type badge, ``#12`` / key / ``[3]``), Qiymat
  (scalar token colored by type, container summary ``{5} id: 1, …``), Bolalar (``?`` while
  unknown), Hajm (tooltip: exact size and offsets), hidden Offset.
* **Data**: a :class:`~jsonreshape.gui.loader.RowLoader` keyed by key path; its fetch
  (:meth:`jsonreshape.gui.treemodel.NodeSource.fetch`) runs in the loader's worker. The GUI
  thread never calls a ``Node`` method (§11.0). Child counts of large nodes (> 1 MiB or
  ``is_large``) come from a "children" task (progress, cancel in the tasks panel).
* **Expansion state** is kept across filter / sort changes (``top_pos`` of non-base views from
  a batched position lookup in a silent task, cached per view; only expanded records are
  looked up).
* Expand all (``*``, warns above 1000 rows, hard cap 200 000), collapse all, expand to level
  1–9, view-only node sorting (object keys alphabetically, array elements by a field), quick
  find in the loaded rows (Ctrl+F, F3 / Shift+F3), breadcrumb, full value window (Space),
  node context menu.

Deviations from §11.7 (documented): positions of expanded records in non-base views are
looked up in one batched silent task (instead of one ``session.position_of`` call per record:
thousands of expanded records would start thousands of tasks; while a filter still grows, a
record not found yet is asked again at most every :data:`POS_RETRY_MS` after the view grew,
and positions of rows already shown are remembered, so they need no lookup); the element
sort uses ``sortkeys.encode_token`` on ``core.PathExtractor`` tokens (identical to
``encode_value`` of the parsed values by the sortkeys contract).

Display orders of node sorting are computed *and inverted* in the workers
(:class:`~jsonreshape.gui.flattree.DisplayOrder`; the sort itself is
:func:`~jsonreshape.gui.treemodel.stable_argsort`, in small GIL-friendly steps): expanding or
re-ordering a sorted node with a million children is ``O(1)`` work on the GUI thread. The
"expand many rows?" question of expand all / expand to level is a window-modal message box
opened with ``open()`` (no nested event loop inside a task callback); the plan is applied
once it is answered (and only if nothing newer replaced it).
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections import OrderedDict
from collections.abc import Callable, Iterator
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QKeyEvent, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QRadioButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jsonreshape.errors import JsonReshapeError
from jsonreshape.gui import actions, copyfmt, quickfind, theme, widgets
from jsonreshape.gui.flattree import DisplayOrder, FlatTree, RowInfo
from jsonreshape.gui.loader import LoadFailed, RowLoader
from jsonreshape.gui.session import NodeRef, find_position, same_node
from jsonreshape.gui.treemodel import (
    ROOT,
    WRAPPER,
    ExpandPlan,
    NodeInfo,
    NodeSource,
    count_badge,
    display_key,
    find_matches,
    key_label,
    search_texts,
)
from jsonreshape.gui.vgrid import Cell, Column, VirtualGrid
from jsonreshape.i18n import t
from jsonreshape.sortkeys import SortKey
from jsonreshape.tasks import check

log = logging.getLogger(__name__)

__all__ = ["Breadcrumb", "ElementSortDialog", "TreeView", "EXPAND_WARN_ROWS", "EXPAND_CAP_ROWS",
           "QUICK_SCAN_ROWS"]

EXPAND_WARN_ROWS = 1000        # expand all / to level: confirm above this many rows
EXPAND_CAP_ROWS = 200_000      # ... and refuse above this many
QUICK_SCAN_ROWS = 50_000       # quick find looks at most at this many rows
QUICK_MAX_MATCHES = 20_000
ORDER_CACHE_ENTRIES = 8_000_000   # child indices kept in the display-order cache
KNOWN_INFOS = 8192             # infos from tasks (ancestors of revealed nodes)
SMALL_ORDER = 20_000           # a reveal computes display orders up to this many children itself
BIG_TASK_CHILDREN = 50_000     # order computations above this are visible tasks
POS_RETRY_MS = 500             # a growing view: records not found yet are asked again at most this often
SEEN_POSITIONS = 16384         # view positions of top rows already shown (non-base views)

_Callbacks = list[tuple[Callable[[], Any] | None, Callable[[], Any] | None]]   # (then, otherwise)

COL_KEY, COL_VALUE, COL_CHILDREN, COL_SIZE, COL_OFFSET = "key", "value", "children", "size", "offset"
KEY_WIDTH = 200                # default width of the key column
MIN_VALUE_WIDTH = 160          # the value column fills the rest of the pane, at least this wide

def _seg_text(seg: Any) -> str:
    if isinstance(seg, int) and not isinstance(seg, bool):
        return f"[{seg}]"
    return display_key(str(seg))


def _path_label(path: tuple) -> str:
    text = copyfmt.path_text(path, "dotted") if path else ""
    return copyfmt.escape_surrogates(text) if text else "$"


# ==================================================================================
# breadcrumb
# ==================================================================================
class Breadcrumb(QWidget):
    """The path of the current node above the tree: ``$ › data › #12 › contact › city``.
    Every segment is clickable (``segment_clicked(index)``); long paths are elided in the
    middle (a ``…`` button lists the hidden segments); a copy button
    (``copy_requested``)."""

    segment_clicked = Signal(int)
    copy_requested = Signal()
    MAX_SEGMENT_PX = 260

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Breadcrumb")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._items: list[tuple[str, bool]] = []
        self._buttons: list[QToolButton] = []
        self._seps: list[QLabel] = []
        self._hidden: list[int] = []
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(0)
        self._row = QHBoxLayout()
        self._row.setSpacing(0)
        self._row.addStretch(1)   # also without segments: the copy button stays at the right
        lay.addLayout(self._row, 1)
        self.more_button = QToolButton(self)
        self.more_button.setText("…")
        self.more_button.setAutoRaise(True)
        self.more_button.setToolTip(t("gui.tree.crumb.hidden"))
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_button.hide()
        self.copy_button = QToolButton(self)
        self.copy_button.setText(t("gui.tree.crumb.copy"))
        self.copy_button.setToolTip(t("gui.tree.crumb.copy.tip"))
        self.copy_button.setAutoRaise(True)
        self.copy_button.clicked.connect(self.copy_requested.emit)
        lay.addWidget(self.copy_button)
        h = max(self.fontMetrics().height() + 10, self.copy_button.sizeHint().height() + 4)
        self.setFixedHeight(h)

    # -- content -----------------------------------------------------------------------
    def set_segments(self, items: list[tuple[str, bool]]) -> None:
        """``[(text, clickable)]`` from the root to the current node."""
        items = [(copyfmt.escape_surrogates(str(text)), bool(ok)) for text, ok in items]
        if items == self._items:
            return
        self._items = items
        for w in self._buttons + self._seps:
            self._row.removeWidget(w)
            w.deleteLater()
        self._buttons, self._seps = [], []
        self._row.removeWidget(self.more_button)
        while self._row.count():
            self._row.takeAt(0)
        fm = self.fontMetrics()
        muted = self._muted_palette()
        for i, (text, ok) in enumerate(items):
            if i:
                sep = QLabel("›", self)
                sep.setPalette(muted)
                sep.setContentsMargins(2, 0, 2, 0)
                self._seps.append(sep)
            btn = QToolButton(self)
            shown = fm.elidedText(text, Qt.TextElideMode.ElideMiddle, self.MAX_SEGMENT_PX)
            btn.setText(shown.replace("&", "&&"))
            btn.setAutoRaise(True)
            btn.setToolTip(text if shown != text else "")
            btn.setEnabled(ok)
            btn.setProperty("segment", i)
            btn.clicked.connect(lambda _checked=False, k=i: self.segment_clicked.emit(k))
            if i == len(items) - 1:
                font = QFont(btn.font())
                font.setBold(True)
                btn.setFont(font)
            self._buttons.append(btn)
        self._relayout()

    def _muted_palette(self) -> QPalette:
        muted = QPalette(self.palette())
        muted.setColor(QPalette.ColorRole.WindowText, theme.color("muted"))
        return muted

    def update_colors(self) -> None:
        """Re-color the separators after a theme change (they keep the muted color of the
        theme they were made in otherwise)."""
        muted = self._muted_palette()
        for sep in self._seps:
            sep.setPalette(muted)

    def separator_color(self) -> QColor | None:
        """Text color of the separators (``None`` without separators; tests)."""
        return QColor(self._seps[0].palette().color(QPalette.ColorRole.WindowText)) if self._seps else None

    def segments(self) -> list[str]:
        return [text for text, _ok in self._items]

    def hidden_segments(self) -> list[int]:
        """Indices of the segments elided into the ``…`` button."""
        return list(self._hidden)

    def click(self, index: int) -> None:
        """Act as if segment ``index`` was clicked (when it is clickable)."""
        if 0 <= index < len(self._items) and self._items[index][1]:
            self.segment_clicked.emit(index)

    # -- layout ------------------------------------------------------------------------
    def _relayout(self) -> None:
        while self._row.count():
            self._row.takeAt(0)
        n = len(self._buttons)
        widths = [b.sizeHint().width() for b in self._buttons]
        sep_w = self._seps[0].sizeHint().width() if self._seps else 0
        avail = max(0, self.width() - self.copy_button.sizeHint().width() - 16)
        total = sum(widths) + sep_w * max(0, n - 1)
        hidden: list[int] = []
        if n > 2 and total > avail:
            more_w = self.more_button.sizeHint().width() + sep_w
            used = widths[0] + more_w
            keep_from = n - 1
            used += widths[-1] + sep_w
            while keep_from > 1 and used + widths[keep_from - 1] + sep_w <= avail:
                keep_from -= 1
                used += widths[keep_from] + sep_w
            hidden = list(range(1, keep_from))
        self._hidden = hidden
        menu = QMenu(self.more_button)
        for i in hidden:
            act = menu.addAction(self._items[i][0].replace("&", "&&"))
            act.setEnabled(self._items[i][1])
            act.triggered.connect(lambda _checked=False, k=i: self.segment_clicked.emit(k))
        old = self.more_button.menu()
        self.more_button.setMenu(menu if hidden else None)
        if old is not None:
            old.deleteLater()
        for i, btn in enumerate(self._buttons):
            if i in hidden:
                btn.hide()
                if i - 1 < len(self._seps) and i >= 1:
                    self._seps[i - 1].hide()
                if i == hidden[0]:
                    self._row.addWidget(self._seps[i - 1])
                    self._seps[i - 1].show()
                    self._row.addWidget(self.more_button)
                    self.more_button.show()
                continue
            if i:
                self._row.addWidget(self._seps[i - 1])
                self._seps[i - 1].show()
            self._row.addWidget(btn)
            btn.show()
        if not hidden:
            self.more_button.hide()
        self._row.addStretch(1)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        if self._buttons:
            self._relayout()

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return QSize(200, self.height())


# ==================================================================================
# element sort dialog
# ==================================================================================
class ElementSortDialog(QDialog):
    """Choose the field (and direction) the elements of an array are shown sorted by."""

    def __init__(self, fields: list[str], parent: QWidget | None = None, *, path: str = "",
                 current: tuple[str, bool] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(t("gui.tree.sortdlg.title"))
        lay = QVBoxLayout(self)
        if path:
            head = QLabel(path, self)
            head.setTextFormat(Qt.TextFormat.PlainText)
            lay.addWidget(head)
        row = QHBoxLayout()
        row.addWidget(QLabel(t("gui.tree.sortdlg.field"), self))
        self.field_combo = QComboBox(self)
        self.field_combo.setEditable(True)
        self.field_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.field_combo.addItems(fields)
        self.field_combo.setMinimumWidth(240)
        self.field_combo.setToolTip(t("gui.tree.sortdlg.field.tip"))
        row.addWidget(self.field_combo, 1)
        lay.addLayout(row)
        self.asc_radio = QRadioButton(t("gui.tree.sortdlg.asc"), self)
        self.desc_radio = QRadioButton(t("gui.tree.sortdlg.desc"), self)
        group = QButtonGroup(self)
        group.addButton(self.asc_radio)
        group.addButton(self.desc_radio)
        self.asc_radio.setChecked(True)
        dirs = QHBoxLayout()
        dirs.addWidget(self.asc_radio)
        dirs.addWidget(self.desc_radio)
        dirs.addStretch(1)
        lay.addLayout(dirs)
        note = QLabel(t("gui.tree.sortdlg.note"), self)
        note.setWordWrap(True)
        lay.addWidget(note)
        buttons = QDialogButtonBox(self)
        buttons.addButton(t("gui.common.ok"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(t("gui.common.cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        if current is not None:
            self.field_combo.setEditText(current[0])
            (self.desc_radio if current[1] else self.asc_radio).setChecked(True)

    def field(self) -> str:
        return self.field_combo.currentText().strip()

    def descending(self) -> bool:
        return self.desc_radio.isChecked()


# ==================================================================================
# grid source
# ==================================================================================
class _GridSource:
    """The :class:`~jsonreshape.gui.vgrid.GridSource` of the tree (delegates to the view)."""

    def __init__(self, view: TreeView) -> None:
        self._view = view

    def row_count(self) -> int:
        return self._view.flat.row_count()

    def cell(self, row: int, column: Column) -> Cell | None:
        return self._view._cell(row, column)

    def rows_shown(self, first: int, last: int) -> None:
        self._view._rows_shown(first, last)

    def row_label(self, row: int) -> str | None:
        return None


# ==================================================================================
# the view
# ==================================================================================
class TreeView(widgets.ViewBase):
    """The tree of one document (see the module docstring)."""

    view_id = "tree"
    selection_changed = Signal()     # the current row changed (status bar)
    structure_changed = Signal()     # rows were added / removed / reordered

    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(session, parent)
        self._dead = False
        self._active = False
        self._doc: Any = None
        self._src: NodeSource | None = None
        self._kind = ""
        self.flat = FlatTree()
        self._row_cache: dict[int, RowInfo] = {}
        self._depth = 0                 # nesting of _structure()
        self._syncing = False           # grid signals caused by us are ignored
        self._cur_kp: tuple | None = None
        self._own_ref: NodeRef | None = None
        self._pending_select: tuple | None = None
        self._pending_expand: dict[tuple, _Callbacks] = {}
        self._busy: dict[tuple, _Callbacks] = {}
        self._known: OrderedDict[tuple, NodeInfo] = OrderedDict()
        self._follow_pending: NodeRef | None = None
        self._root_auto = False
        self._reveal_gen = 0
        self._reveal_ref: NodeRef | None = None    # the reveal on its way (cleared when it ends in any way)
        # positions of top keys in non-base views
        self._view_gen = 0
        self._pos_cache: dict[int, int | None] = {}
        self._pos_asked: set[int] = set()
        self._pos_wanted: dict[int, list[Callable[[int | None], Any]]] = {}
        self._pos_task: Any = None
        self._pos_retry: set[int] = set()          # not found in a growing view yet: asked again later
        self._seen_pos: OrderedDict[int, int] = OrderedDict()   # record -> view position of rows shown
        self._peek = 0                             # > 0: top_pos never starts a lookup
        self._remap_timer = QTimer(self)
        self._remap_timer.setSingleShot(True)
        self._remap_timer.setInterval(0)
        self._remap_timer.timeout.connect(self._remap)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(POS_RETRY_MS)
        self._retry_timer.timeout.connect(self._retry_positions)
        # node sorting
        self._sort_keys = False
        self._elem_sorts: dict[tuple, tuple[str, bool]] = {}
        self._orders: OrderedDict[tuple, tuple[str, DisplayOrder]] = OrderedDict()
        self._order_entries = 0
        self._kinds: dict[tuple, str] = {}     # kind of expanded nodes
        self._plan_task: Any = None
        self._plan_gen = 0
        self._plan_box: QMessageBox | None = None   # "expand many rows?" (non-modal open())
        self._plan_auto = False                     # an automatic answer (confirm_handler) is pending
        self._sort_dialog: ElementSortDialog | None = None
        # quick find
        self._qf_matches: dict[tuple, tuple[tuple, tuple]] = {}
        self._qf_current: tuple | None = None
        self._qf_capped = False
        self._qf_gen = 0
        self._qf_task: Any = None
        self._qf_timer = QTimer(self)
        self._qf_timer.setSingleShot(True)
        self._qf_timer.setInterval(300)
        self._qf_timer.timeout.connect(self._qf_rescan)
        self.last_message = ""
        self._placeholder = t("gui.tree.placeholder.opening")
        self._fitting = False           # the view sets column widths (not the user)
        self._user_widths = False       # the user resized a column: stop fitting the value column
        self._fit_widths: dict[str, int] = {}
        self._crumb_refs: list[NodeRef | None] = []

        self._loader = RowLoader(self._fetch, capacity=4096, batch=64, name="tree", parent=self)
        self._loader.loaded.connect(self._on_loaded)
        self._data_font = widgets.mono_font(self.session)
        self._build_ui()
        self._connect()
        self._update_placeholder()   # the session may have failed (or opened) before the view existed

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.breadcrumb = Breadcrumb(self)
        self.breadcrumb.segment_clicked.connect(self._on_crumb)
        self.breadcrumb.copy_requested.connect(self._copy_crumb_path)
        lay.addWidget(self.breadcrumb)
        self.message_bar = widgets.InfoBar("info", "", parent=self)
        self.message_bar.hide()
        lay.addWidget(self.message_bar)
        self.grid = VirtualGrid(self)
        self.grid.setObjectName("treeGrid")
        self.grid.set_columns(self._make_columns())
        self.grid.set_tree_column(0)
        self.grid.set_selection_mode("single")
        self.grid.set_font(QFont(self._data_font))
        self.grid.set_placeholder_text(t("gui.tree.placeholder.opening"))
        self.grid.set_key_handler(self._on_key)
        self.grid.current_changed.connect(self._on_grid_current)
        self.grid.activated.connect(self._on_activated)
        self.grid.expander_clicked.connect(self._on_expander)
        self.grid.context_menu_requested.connect(self._on_context_menu)
        self.grid.header_context_menu.connect(self._on_header_menu)
        self.grid.columns_changed.connect(self._on_columns_changed)
        self.grid.viewport().installEventFilter(self)
        self._grid_source = _GridSource(self)
        self.grid.set_source(self._grid_source)
        lay.addWidget(self.grid, 1)
        self.find_bar = quickfind.QuickFindBar(self)
        self.find_bar.search_changed.connect(self._qf_search)
        self.find_bar.next_requested.connect(lambda: self.find_next(False))
        self.find_bar.prev_requested.connect(lambda: self.find_next(True))
        self.find_bar.closed.connect(self._qf_closed)
        lay.addWidget(self.find_bar)
        self.setFocusProxy(self.grid)

    # -- column widths: the value column takes the rest of the pane ---------------------
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt name)
        if event.type() == QEvent.Type.Resize and obj is self.grid.viewport():
            self.fit_columns()
        return super().eventFilter(obj, event)

    def fit_columns(self) -> None:
        """Give the value column the width the other visible columns leave (until the user
        resizes a column)."""
        if self._user_widths or self._dead:
            return
        cols = self.grid.columns()
        index = next((i for i, c in enumerate(cols) if c.id == COL_VALUE), None)
        if index is None:
            return
        avail = self.grid.viewport().width() - self.grid.gutter_width()
        other = sum(c.width for c in cols if c.visible and c.id != COL_VALUE)
        self._fitting = True
        try:
            self.grid.set_column_width(index, max(MIN_VALUE_WIDTH, avail - other - 1))
        finally:
            self._fitting = False
        self._fit_widths = {c.id: c.width for c in self.grid.columns()}

    def _on_columns_changed(self) -> None:
        if self._fitting:
            return
        now = {c.id: c.width for c in self.grid.columns()}
        if self._fit_widths and now != self._fit_widths:
            self._user_widths = True   # a manual resize: keep the user's widths
        else:
            self.fit_columns()         # shown / hidden / moved columns

    @staticmethod
    def _make_columns() -> list[Column]:
        return [
            Column(COL_KEY, t("gui.tree.col.key"), KEY_WIDTH),
            Column(COL_VALUE, t("gui.tree.col.value"), 380),
            Column(COL_CHILDREN, t("gui.tree.col.children"), 70, align="right"),
            Column(COL_SIZE, t("gui.tree.col.size"), 80, align="right"),
            Column(COL_OFFSET, t("gui.tree.col.offset"), 110, visible=False, align="right"),
        ]

    def _connect(self) -> None:
        s = self.session
        self._conns = [
            (s.opened, self._on_opened),
            (s.count_changed, self._on_count_changed),
            (s.view_changed, self._on_view_changed),
            (s.index_finished, self._on_index_finished),
            (s.current_changed, self._on_current_changed),
            (s.reveal_requested, self._on_reveal_requested),
            (s.state_changed, self._on_state_changed),
            (theme.notifier().theme_changed, self._on_theme_changed),
        ]
        settings_changed = getattr(getattr(s, "app", None), "settings_changed", None)
        if settings_changed is not None:
            self._conns.append((settings_changed, self._on_settings_changed))
        for signal, slot in self._conns:
            signal.connect(slot)

    def _on_settings_changed(self) -> None:
        """The data font follows the settings at once (the zoom level is kept)."""
        if self._dead:
            return
        font = widgets.mono_font(self.session)
        if font == self._data_font:
            return
        self._data_font = QFont(font)
        zoom = self.grid.zoom_level()
        self.grid.set_font(font)
        if zoom:
            self.grid.zoom(zoom)
        self.fit_columns()

    def _on_theme_changed(self) -> None:
        if not self._dead:
            self.breadcrumb.update_colors()

    # ------------------------------------------------------------------ ViewBase hooks
    def activate(self) -> None:
        """Became visible: first load (rows of the current view) and follow the session's
        current node."""
        if self._dead:
            return
        self._active = True
        self._update_placeholder()
        if self._ensure_doc():
            self._sync_top()
        if self._pos_retry and not self._retry_timer.isActive():
            self._retry_timer.start()
        pending = self._follow_pending or getattr(self.session, "current", None)
        self._follow_pending = None
        if pending is not None and self._ready() and not same_node(pending, self._own_ref):
            self.reveal(pending)
        self.grid.refresh(request=True)

    def zoom(self, steps: int) -> None:
        self.grid.zoom(steps)

    def quick_find(self) -> None:
        self.find_bar.open_bar()

    def selection_count(self) -> int:
        return 1 if self.grid.current()[0] >= 0 else 0

    def selected_text(self) -> str:
        """Text of the current node for prefilling a search: a scalar's value (strings
        decoded), else the member / record key ("" when there is none)."""
        kp = self._visible_current()
        info = self._info(kp) if kp is not None else None
        if info is None:
            return ""
        key = info.key if isinstance(info.key, str) else ""
        if info.is_container:
            return key
        text = info.text
        if info.kind == "string":
            if len(text) < 2 or not text.endswith('"'):
                return key   # a cut long string: its key is the better search text
            try:
                value = json.loads(text)
            except ValueError:
                return key
            return value if isinstance(value, str) else key
        return text

    def shutdown(self) -> None:
        if self._dead:
            return
        self._dead = True
        for signal, slot in self._conns:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._remap_timer.stop()
        self._retry_timer.stop()
        self._qf_timer.stop()
        self._close_plan_box()
        self._loader.close()
        super().shutdown()

    # ------------------------------------------------------------------ document
    def _ready(self) -> bool:
        return not self._dead and self._doc is not None and self._doc is self.session.doc

    def _ensure_doc(self) -> bool:
        """Follow the session's document (a reopen gives a new one). True when ready."""
        if self._dead:
            return False
        doc = self.session.doc
        if doc is self._doc:
            return doc is not None
        self._reset_doc(doc)
        return doc is not None

    def _reset_doc(self, doc: Any) -> None:
        try:
            self.session.app.tasks.detach(self)
        except Exception:  # pragma: no cover - defensive
            log.debug("detach failed", exc_info=True)
        self._doc = doc
        self._src = NodeSource(doc) if doc is not None else None
        self._kind = doc.kind if doc is not None else ""
        self._loader.invalidate()
        self.flat = FlatTree()
        self._row_cache.clear()
        self._cur_kp = None
        self._own_ref = None
        self._pending_select = None
        self._pending_expand.clear()
        self._busy.clear()
        self._known.clear()
        self._pos_cache.clear()
        self._pos_asked.clear()
        self._pos_wanted.clear()
        self._pos_retry.clear()
        self._seen_pos.clear()
        self._retry_timer.stop()
        self._pos_task = None
        self._plan_task = None
        self._plan_gen += 1
        self._close_plan_box()
        self._view_gen += 1
        self._reveal_gen += 1
        self._reveal_ref = None
        self._elem_sorts.clear()
        self._orders.clear()
        self._order_entries = 0
        self._kinds.clear()
        self._qf_matches = {}
        self._qf_current = None
        self._root_auto = doc is not None and self._kind == "single"
        count, key, pos = self._top_spec()
        self.flat.set_top(count, top_key=key, top_pos=pos)
        self._update_placeholder()
        self._syncing = True
        try:
            self.grid.reset()
        finally:
            self._syncing = False
        self.breadcrumb.set_segments([])
        if self._root_auto:
            self._loader.get((ROOT,))

    def _update_placeholder(self) -> None:
        s = self.session
        if s.state == "failed" and s.doc is None:
            text = t("gui.tree.placeholder.failed")
        elif s.doc is None or s.state in ("new", "opening"):
            text = t("gui.tree.placeholder.opening")
        else:
            text = t("gui.tree.placeholder.empty")
        self._placeholder = text
        self.grid.set_placeholder_text(text)

    def placeholder_text(self) -> str:
        """The text the empty tree shows (opening / failed / no records)."""
        return self._placeholder

    # ------------------------------------------------------------------ top level
    def _offset(self) -> int:
        return 1 if self._kind == "wrapped" else 0

    def _top_spec(self) -> tuple[int, Callable[[int], Any] | None, Callable[[Any], int | None] | None]:
        """``(top_count, top_key, top_pos)`` of the current view for the FlatTree."""
        if self._doc is None:
            return 0, None, None
        if self._kind == "single":
            return 1, (lambda _p: ROOT), (lambda k: 0 if k == ROOT else None)
        s = self.session
        off = self._offset()
        n = s.view_len()
        if s.view_is_base:
            if not off:
                return n, None, None

            def base_key(p: int) -> Any:
                return WRAPPER if p == 0 else p - 1

            def base_pos(k: Any) -> int | None:
                if k == WRAPPER:
                    return 0
                if isinstance(k, int) and not isinstance(k, bool) and 0 <= k < n:
                    return k + 1
                return None

            return n + 1, base_key, base_pos
        cache = self._pos_cache
        seen = self._seen_pos

        def view_key(p: int) -> Any:
            if off and p == 0:
                return WRAPPER
            return s.record_at(p - off)

        def view_pos(k: Any) -> int | None:
            if k == WRAPPER:
                return 0 if off else None
            if not isinstance(k, int) or isinstance(k, bool):
                return None
            if k in cache:
                v = cache[k]
                return None if v is None or v >= n else v + off
            v = seen.get(k)
            if v is not None:
                return v + off if v < n else None
            if not self._peek:
                self._want_position(k, None)
            return None

        return n + off, view_key, view_pos

    @contextlib.contextmanager
    def _peeking(self) -> Iterator[None]:
        """Row lookups inside never start a position lookup task (records whose view
        position is not known count as not shown)."""
        self._peek += 1
        try:
            yield
        finally:
            self._peek -= 1

    def _note_shown(self, rows: list[RowInfo]) -> None:
        """Remember the view positions of top rows just located (non-base views): rows
        on screen then need no position lookup (quick find, reveal, re-mapping)."""
        if self._kind == "single" or self.session.view_is_base:
            return
        seen = self._seen_pos
        off = self._offset()
        for ri in rows:
            if ri.depth:
                continue
            key = ri.keypath[0]
            if isinstance(key, int) and not isinstance(key, bool):
                seen[key] = ri.pos - off
                seen.move_to_end(key)
        while len(seen) > SEEN_POSITIONS:
            seen.popitem(last=False)

    def _sync_top(self) -> None:
        """Re-map the top level to the session's view (count / order changed); expansion
        is kept."""
        if not self._ready():
            return
        count, key, pos = self._top_spec()
        with self._structure():
            try:
                self.flat.set_top(count, top_key=key, top_pos=pos)
            except ValueError:
                log.debug("stale top positions; asking again", exc_info=True)
                self._pos_cache.clear()
                self._pos_asked.clear()
                self._pos_retry.clear()
                self._seen_pos.clear()
                count, key, pos = self._top_spec()
                self.flat.set_top(count, top_key=key, top_pos=pos)

    def _remap(self) -> None:
        if not self._dead:
            self._sync_top()

    # -- positions of records in non-base views (batched) ------------------------------
    def _want_position(self, record: int, callback: Callable[[int | None], Any] | None) -> None:
        """Ask for the view position of ``record`` (``callback(pos | None)`` in the GUI
        thread; ``None`` callback: re-map when it arrives)."""
        if self.session.view_is_base:
            n = self.session.view_len()
            pos = record if 0 <= record < n else None
            if callback is not None:
                QTimer.singleShot(0, self, lambda: callback(pos))
            return
        if record in self._pos_cache:
            if callback is not None:
                cached = self._pos_cache[record]
                QTimer.singleShot(0, self, lambda: callback(cached))
            return
        if callback is None and record in self._pos_asked:
            return
        waiting = self._pos_wanted.setdefault(record, [])
        if callback is not None:
            waiting.append(callback)
        self._pos_asked.add(record)
        if self._pos_task is None:
            QTimer.singleShot(0, self, self._start_position_task)

    def _start_position_task(self) -> None:
        if self._dead or self._pos_task is not None or not self._pos_wanted or not self._ready():
            return
        rs = self.session.view_recordset()
        if rs is None:
            return
        records = list(self._pos_wanted)
        gen = self._view_gen

        def work(ctx: Any) -> tuple[dict[int, int | None], bool]:
            complete = bool(getattr(rs, "complete", True))
            found: dict[int, int | None] = {}
            if rs.is_ascending():
                for r in records:
                    found[r] = find_position(rs, r, ctx.cancel)
            else:
                wanted = set(records)
                pos = 0
                for chunk in rs.iter_chunks():
                    check(ctx.cancel)
                    hits = wanted.intersection(chunk)
                    for r in hits:
                        found[r] = pos + chunk.index(r)
                    wanted -= hits
                    if not wanted:
                        break
                    pos += len(chunk)
            for r in records:
                found.setdefault(r, None)
            return found, complete

        self._pos_task = self.session.app.tasks.run(
            work, title=t("gui.tree.task.locate"), kind="locate", owner=self, silent=True,
            on_done=lambda result: self._positions_done(gen, result),
            on_error=lambda exc: self._positions_done(gen, ({r: None for r in records}, False)),
            on_cancel=lambda: self._positions_done(gen, None))

    def _positions_done(self, gen: int, result: tuple[dict[int, int | None], bool] | None) -> None:
        self._pos_task = None
        if self._dead:
            return
        if result is None or gen != self._view_gen:
            QTimer.singleShot(0, self, self._start_position_task)   # the view changed: ask again
            return
        found, complete = result
        callbacks: list[tuple[Callable[[int | None], Any], int | None]] = []
        for record, pos in found.items():
            if pos is not None or complete:
                self._pos_cache[record] = pos
                self._pos_retry.discard(record)
            else:
                # a growing view that does not hold the record yet: it stays "asked" (no
                # new task at every re-map) and is asked again once the view grew, at most
                # every POS_RETRY_MS (_on_count_changed); a complete view answers for good
                self._pos_retry.add(record)
            for cb in self._pos_wanted.pop(record, []):
                callbacks.append((cb, pos))
        self._remap_timer.start()
        for cb, pos in callbacks:
            try:
                cb(pos)
            except Exception:  # pragma: no cover - defensive
                log.exception("position callback failed")
        if self._pos_wanted:
            QTimer.singleShot(0, self, self._start_position_task)

    def _retry_positions(self) -> None:
        """Ask again for the records a growing view did not hold at the last lookup."""
        if self._dead or not self._pos_retry or not self._ready():
            return
        self._pos_asked.difference_update(self._pos_retry)
        self._pos_retry.clear()
        self._sync_top()    # asks for the recorded (expanded) ones in one batched task

    # ------------------------------------------------------------------ session signals
    def _on_opened(self) -> None:
        if self._dead:
            return
        self._update_placeholder()
        if self._active:
            self._ensure_doc()
            self.grid.refresh(request=True)

    def _on_state_changed(self, _state: str) -> None:
        if self._dead:
            return
        self._update_placeholder()
        if self.session.doc is None and self._doc is not None:
            self._reset_doc(None)

    def _on_count_changed(self) -> None:
        if self._dead or not self._active:
            return
        if not self._ensure_doc():
            return
        self._sync_top()
        if self._pos_retry and not self._retry_timer.isActive():
            self._retry_timer.start()   # the view grew: look again, throttled

    def _on_view_changed(self) -> None:
        if self._dead:
            return
        self._view_gen += 1
        self._pos_cache.clear()
        self._pos_asked.clear()
        self._pos_retry.clear()
        self._seen_pos.clear()
        self._retry_timer.stop()
        if self._pos_task is not None:
            self._pos_task.cancel()
        if not self._active:
            return
        if self.session.doc is not self._doc:
            self._ensure_doc()
            return
        self._sync_top()
        self._update_placeholder()

    def _on_index_finished(self) -> None:
        if self._dead or not self._active:
            return
        if self._ensure_doc():
            self._sync_top()
            self.grid.refresh()

    def _on_current_changed(self, ref: Any, origin: Any) -> None:
        if self._dead or origin is self or ref is None:
            return
        if not self._active or not self.isVisible():
            self._follow_pending = ref
            return
        if same_node(ref, self._own_ref):
            return
        self.reveal(ref)

    def _on_reveal_requested(self, ref: Any) -> None:
        if self._dead or ref is None:
            return
        if not self._active:
            self._follow_pending = ref
            return
        if self._reveal_ref is not None and same_node(ref, self._reveal_ref):
            return   # already on its way (current_changed came first)
        self.reveal(ref)

    # ------------------------------------------------------------------ loader
    def _fetch(self, keys: list, cancel: Any) -> dict:
        src = self._src
        if src is None:
            return {}
        out = src.fetch(keys, cancel)
        return {k: (LoadFailed(v) if isinstance(v, BaseException) else v) for k, v in out.items()}

    def _info(self, kp: tuple) -> NodeInfo | None:
        value = self._loader.peek(kp)
        if isinstance(value, NodeInfo):
            return value
        return self._known.get(kp)

    def _remember(self, info: NodeInfo) -> None:
        self._known[info.keypath] = info
        self._known.move_to_end(info.keypath)
        while len(self._known) > KNOWN_INFOS:
            self._known.popitem(last=False)

    def _on_loaded(self) -> None:
        if self._dead:
            return
        self.grid.refresh()
        if self.find_bar.isVisible() and self.find_bar.text():
            self._qf_timer.start()      # rows came into view (scrolling): find in them too (debounced)
        if self._pending_select is not None and self._info(self._pending_select) is not None:
            kp = self._pending_select
            self._pending_select = None
            if kp == self._cur_kp:
                self._select_kp(kp)
        if self._pending_expand:
            for kp in list(self._pending_expand):
                if self._info(kp) is not None:
                    callbacks = self._pending_expand.pop(kp)
                    self.expand_node(kp, then=_chain(callbacks), otherwise=_chain(callbacks, failed=True))
                elif isinstance(self._loader.peek(kp), LoadFailed):
                    _chain(self._pending_expand.pop(kp), failed=True)()
        if self._root_auto:
            info = self._info((ROOT,))
            if info is not None:
                self._root_auto = False
                if info.expandable and not self.flat.is_expanded((ROOT,)):
                    self.expand_node((ROOT,))

    # ------------------------------------------------------------------ grid source
    def _locate(self, row: int) -> RowInfo:
        ri = self._row_cache.get(row)
        if ri is None:
            if len(self._row_cache) > 8192:
                self._row_cache.clear()
            ri = self.flat.locate(row)
            self._row_cache[row] = ri
        return ri

    def _rows_shown(self, first: int, last: int) -> None:
        if not self._ready():
            return
        if len(self._row_cache) > 8192:
            self._row_cache.clear()
        keys = []
        rows = list(self.flat.iter_rows(first, last + 1))
        for ri in rows:
            self._row_cache[ri.row] = ri
            keys.append(ri.keypath)
        self._note_shown(rows)
        self._loader.request(keys)

    def _live_count(self) -> int:
        doc = self._doc
        try:
            return int(doc.record_count) if doc is not None else 0
        except Exception:  # pragma: no cover - a closed document
            return 0

    def _cell(self, row: int, column: Column) -> Cell | None:
        if not self._ready():
            return None
        ri = self._locate(row)
        kp = ri.keypath
        value = self._loader.get(kp)
        if value is None:
            return None
        cid = column.id
        if isinstance(value, LoadFailed):
            if cid == COL_KEY:
                if len(kp) > 1:
                    label = f"[{kp[-1]}]"
                elif isinstance(kp[0], int):
                    label = f"#{kp[0]}"
                else:
                    label = "$" if kp[0] == ROOT else t("gui.tree.wrapper")
                return Cell(text=label, role="error", depth=ri.depth, tooltip=value.text)
            if cid == COL_VALUE:
                return Cell(text=t("gui.tree.load_failed", error=value.text), role="error", tooltip=value.text)
            return Cell()
        info: NodeInfo = value
        match = self._qf_matches.get(kp) if self._qf_matches else None
        current = match is not None and kp == self._qf_current
        if cid == COL_KEY:
            text = key_label(kp, info) or ""
            role: str | None = "key"
            italic = False
            tooltip = None
            top = kp[0]
            if len(kp) == 1:
                if top == WRAPPER:
                    role, italic, tooltip = "muted", True, t("gui.tree.wrapper.tip")
                elif top == ROOT:
                    role = "muted"
                elif isinstance(info.key, str):
                    tooltip = t("gui.tree.record.tip", record=f"#{top}")
                else:
                    role = None
            elif not isinstance(info.key, str):
                role = "muted"
            if info.records:
                tooltip = t("gui.tree.records.tip")
            spans = _spans(match[0], current) if match else ()
            return Cell(text=text, role=role, badge=info.kind, depth=ri.depth, expander=self._expander(kp, info),
                        spans=spans, italic=italic, tooltip=tooltip)
        if cid == COL_VALUE:
            if info.records:
                n = self._live_count()
                text = f"[{widgets.format_count(n)}] " + t("gui.tree.records_below")
                return Cell(text=text, role="muted", italic=True, tooltip=t("gui.tree.records.tip"))
            spans = _spans(match[1], current) if match else ()
            role = "muted" if info.is_container else info.kind
            return Cell(text=info.text, role=role, spans=spans)
        if cid == COL_CHILDREN:
            if not info.is_container:
                return Cell()
            if info.records:
                return Cell(text=widgets.format_count(self._live_count()), align="right", role="muted")
            if info.count is None:
                return Cell(text="?", align="right", role="muted", tooltip=t("gui.tree.count.unknown.tip"))
            return Cell(text=widgets.format_count(info.count), align="right")
        if cid == COL_SIZE:
            return Cell(text=widgets.format_size(info.size), align="right", role="muted",
                        tooltip=t("gui.tree.size.tip", bytes=widgets.format_count(info.size),
                                  start=widgets.format_count(info.start), end=widgets.format_count(info.end)))
        if cid == COL_OFFSET:
            return Cell(text=str(info.start), align="right", role="muted")
        return Cell()

    def _expander(self, kp: tuple, info: NodeInfo) -> int:
        if not info.expandable:
            return 0
        if kp in self._busy:
            return 3
        return 2 if self.flat.is_expanded(kp) else 1

    # ------------------------------------------------------------------ structure changes
    @contextlib.contextmanager
    def _structure(self) -> Iterator[None]:
        """Wrap FlatTree changes: afterwards the grid re-reads the rows, the current node
        keeps (or moves to its nearest visible ancestor) and visible rows are requested."""
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if self._depth == 0:
                self._apply_structure()

    def _apply_structure(self) -> None:
        self._row_cache.clear()
        reselect: tuple | None = None
        self._syncing = True
        try:
            self.grid.rows_changed(keep_current=False)
            kp = self._cur_kp
            if kp is not None:
                row = self.flat.row_of(kp)
                if row is None:
                    for d in range(len(kp) - 1, 0, -1):
                        row = self.flat.row_of(kp[:d])
                        if row is not None:
                            reselect = kp[:d]
                            break
                if row is not None:
                    self.grid.set_current(row, 0, select=True, scroll="none")
        finally:
            self._syncing = False
        if reselect is not None:
            self._cur_kp = reselect
            self._select_kp(reselect)
        self._update_placeholder()
        self.grid.refresh(request=True)
        self.selection_changed.emit()
        self.structure_changed.emit()
        if self.find_bar.isVisible() and self.find_bar.text():
            self._qf_timer.start()

    def _flat_expand(self, kp: tuple, count: int, order: Any, kind: str | None) -> bool:
        """Expand in the FlatTree (inside ``_structure``); False when it is not possible."""
        try:
            self.flat.expand(kp, count, order)
        except (IndexError, ValueError) as exc:
            log.debug("cannot expand %r: %s", kp, exc)
            return False
        if kind:
            self._kinds[kp] = kind
        return True

    # ------------------------------------------------------------------ display orders
    def _store_order(self, kp: tuple, mode: str, order: DisplayOrder) -> None:
        old = self._orders.pop(kp, None)
        if old is not None:
            self._order_entries -= len(old[1])
        self._orders[kp] = (mode, order)
        self._order_entries += len(order)
        while self._order_entries > ORDER_CACHE_ENTRIES and len(self._orders) > 1:
            _k, (_m, arr) = self._orders.popitem(last=False)
            self._order_entries -= len(arr)

    def _drop_order(self, kp: tuple) -> None:
        old = self._orders.pop(kp, None)
        if old is not None:
            self._order_entries -= len(old[1])

    def _order_need(self, kp: tuple, kind: str, count: int | None) -> tuple[Any, bool]:
        """``(order, must_compute)`` for expanding ``kp``."""
        cached = self._orders.get(kp)
        if kp in self._elem_sorts:
            if cached is not None and cached[0] == "elements" and (count is None or len(cached[1]) == count):
                return cached[1], False
            return None, True
        if self._sort_keys and kind == "object":
            if cached is not None and cached[0] == "keys" and (count is None or len(cached[1]) == count):
                return cached[1], False
            if count is not None and count <= 1:
                return None, False
            return None, True
        return None, False

    # ------------------------------------------------------------------ expand / collapse
    def expand_node(self, kp: tuple, *, then: Callable[[], Any] | None = None,
                    otherwise: Callable[[], Any] | None = None) -> bool:
        """Expand a node (asynchronously when its children must be indexed or ordered
        first); ``then()`` runs once it is expanded, ``otherwise()`` when it cannot be (not
        expandable, no children, the row or the children task failed or was cancelled).
        True when done at once."""
        kp = tuple(kp)
        if not self._ready():
            _call(otherwise)
            return False
        if self.flat.is_expanded(kp):
            _call(then)
            return True
        if kp in self._busy:
            self._busy[kp].append((then, otherwise))
            return False
        info = self._info(kp)
        if info is None:
            self._pending_expand.setdefault(kp, []).append((then, otherwise))
            self._loader.get(kp)
            return False
        if not info.expandable:
            _call(otherwise)
            return False
        order, compute = self._order_need(kp, info.kind, info.count)
        if info.count is not None and not compute:
            with self._structure():
                ok = self._flat_expand(kp, info.count, order, info.kind)
            _call(then if ok else otherwise)
            return ok
        self._children_task(kp, info, [(then, otherwise)])
        return False

    def _children_task(self, kp: tuple, info: NodeInfo, callbacks: _Callbacks) -> None:
        src = self._src
        assert src is not None
        doc = self._doc
        elem = self._elem_sorts.get(kp)
        keys = self._sort_keys and info.kind == "object"
        need_count = info.count is None
        path = _path_label(info.path)
        if need_count:
            title = t("gui.tree.task.children", path=path)
        elif elem is not None:
            title = t("gui.tree.task.sort_elements", path=path)
        else:
            title = t("gui.tree.task.sort_keys", path=path)
        visible = need_count or info.large or (info.count or 0) > BIG_TASK_CHILDREN

        def work(ctx: Any) -> tuple[int, DisplayOrder | None, str]:
            node = src.node(kp, ctx.cancel)
            count = int(node.child_count(progress=ctx.progress, cancel=ctx.cancel))
            if elem is None and not (keys and count > 1):
                return count, None, ""
            _count_progress(ctx.progress, count, t("gui.tree.task.sorting"))
            if elem is not None:
                spec = SortKey(elem[0], descending=elem[1])
                order = src.element_order(node, count, spec, ctx.cancel, ctx.progress)
                mode = "elements"
            else:
                order = src.key_order(node, count, ctx.cancel, ctx.progress)
                mode = "keys"
            return count, DisplayOrder(order, count), mode   # inverted here, not on the GUI thread

        def done(result: tuple[int, DisplayOrder | None, str]) -> None:
            callbacks = self._busy.pop(kp, [])
            if self._dead or self._doc is not doc:
                return
            count, order, mode = result
            for inf in (info, self._info(kp)):
                if inf is not None:
                    _set_count(inf, count)
            if order is not None:
                self._store_order(kp, mode, order)
            if count == 0:
                self.grid.refresh()
                _chain(callbacks, failed=True)()
                return
            with self._structure():
                ok = self._flat_expand(kp, count, order, info.kind)
            _chain(callbacks, failed=not ok)()

        def failed(exc: BaseException) -> None:
            callbacks = self._busy.pop(kp, [])
            if self._dead:
                return
            self.grid.refresh()
            widgets.report_task_error(self.session, exc, self, purpose="view")
            _chain(callbacks, failed=True)()

        def cancelled() -> None:
            callbacks = self._busy.pop(kp, [])
            if not self._dead:
                self.grid.refresh()
                _chain(callbacks, failed=True)()

        self._busy[kp] = list(callbacks)
        self.session.app.tasks.run(work, title=title, kind="children", owner=self, silent=not visible,
                                   on_done=done, on_error=failed, on_cancel=cancelled)
        self.grid.refresh()

    def collapse_node(self, kp: tuple) -> bool:
        """Collapse a node (its expanded descendants are remembered)."""
        kp = tuple(kp)
        if not self.flat.is_expanded(kp):
            return False
        with self._structure():
            self.flat.collapse(kp)
        return True

    def toggle_node(self, kp: tuple) -> None:
        kp = tuple(kp)
        if kp in self._busy:
            return
        if self.flat.is_expanded(kp) and self.flat.row_of(kp) is not None:
            self.collapse_node(kp)
        else:
            self.expand_node(kp)

    # -- by row (tests, keyboard) -------------------------------------------------------
    def row_keypath(self, row: int) -> tuple:
        return self._locate(row).keypath

    def row_of(self, kp: tuple) -> int | None:
        return self.flat.row_of(tuple(kp))

    def row_count(self) -> int:
        return self.flat.row_count()

    def info_at(self, row: int) -> NodeInfo | None:
        try:
            return self._info(self.row_keypath(row))
        except IndexError:
            return None

    def cell_text(self, row: int, column_id: str) -> str | None:
        """Text of a cell as painted (``None`` while not loaded)."""
        col = next((c for c in self.grid.columns() if c.id == column_id), None)
        if col is None:
            raise KeyError(column_id)
        cell = self._cell(row, col)
        return None if cell is None else cell.text

    def cell_at(self, row: int, column_id: str) -> Cell | None:
        col = next((c for c in self.grid.columns() if c.id == column_id), None)
        if col is None:
            raise KeyError(column_id)
        return self._cell(row, col)

    def expand_row(self, row: int) -> bool:
        return self.expand_node(self.row_keypath(row))

    def collapse_row(self, row: int) -> bool:
        return self.collapse_node(self.row_keypath(row))

    def toggle_row(self, row: int) -> None:
        self.toggle_node(self.row_keypath(row))

    def select_row(self, row: int, *, scroll: str = "ensure") -> None:
        """Make ``row`` the current row (selects its node in the session)."""
        self.grid.set_current(row, 0, select=True, scroll=scroll)

    def current_keypath(self) -> tuple | None:
        return self._cur_kp

    def busy(self) -> bool:
        """Children / plan / reveal work or row loads are pending (an unanswered "expand N
        rows?" question counts as pending plan work)."""
        return bool(self._busy or self._pending_expand or self._loader.pending() or self._plan_task is not None
                    or self._pos_task is not None or self._plan_auto or self._plan_box is not None)

    def is_loading(self, kp: tuple) -> bool:
        return tuple(kp) in self._busy

    # ------------------------------------------------------------------ selection
    def _ref_for(self, kp: tuple, info: NodeInfo) -> NodeRef:
        top = kp[0]
        ipath = tuple(kp[1:])
        if top == WRAPPER or top == ROOT:
            return NodeRef(None, tuple(info.path), ipath)
        depth = self._src.record_depth if self._src is not None else 1
        return NodeRef(top, tuple(info.path[depth:]), ipath)

    def ref_at(self, row: int) -> NodeRef | None:
        """The NodeRef of a loaded row (``None`` while not loaded)."""
        try:
            kp = self.row_keypath(row)
        except IndexError:
            return None
        info = self._info(kp)
        return None if info is None else self._ref_for(kp, info)

    def _kp_for_ref(self, ref: NodeRef) -> tuple | None:
        path = tuple(ref.path)
        ipath = ref.ipath
        if ipath is None or len(ipath) != len(path):
            return None
        if ref.record is None or self._kind == "single":
            if self._kind == "single":
                top: Any = ROOT
            elif self._kind == "wrapped":
                top = WRAPPER
            else:
                return None
        else:
            top = int(ref.record)
        return (top,) + tuple(int(i) for i in ipath)

    def _on_grid_current(self, row: Any, _col: int) -> None:
        if self._syncing or self._dead:
            return
        self.selection_changed.emit()
        if row is None or row < 0 or not self._ready():
            return
        try:
            kp = self._locate(row).keypath
        except IndexError:
            return
        if kp == self._cur_kp:
            return
        self._cur_kp = kp
        self._select_kp(kp)

    def _select_kp(self, kp: tuple, *, history: bool = True) -> None:
        info = self._info(kp)
        if info is None:
            self._pending_select = kp
            self._loader.get(kp)
            self._update_breadcrumb(None)
            return
        self._pending_select = None
        ref = self._ref_for(kp, info)
        self._own_ref = ref
        self.session.select(ref, origin=self, history=history)
        self._update_breadcrumb(ref)

    def _set_current_kp(self, kp: tuple, *, scroll: str = "center", select: bool = True,
                        history: bool = True) -> bool:
        row = self.flat.row_of(kp)
        if row is None:
            return False
        self._cur_kp = kp
        self._syncing = True
        try:
            self.grid.set_current(row, 0, select=True, scroll=scroll)
        finally:
            self._syncing = False
        self.selection_changed.emit()
        if select:
            self._select_kp(kp, history=history)
        return True

    # ------------------------------------------------------------------ reveal
    def reveal(self, ref: NodeRef, *, select: bool = False, center: bool = True) -> None:
        """Show ``ref``: resolve it (worker), make sure its record is in the view, expand
        its ancestors (children tasks for large ones), then make it the current row
        (scrolled to the center). ``select``: also select it in the session (a user
        action of this view, e.g. a breadcrumb click)."""
        if self._dead:
            return
        if not self._active or not self._ensure_doc():
            self._follow_pending = ref
            return
        self._reveal_gen += 1
        gen = self._reveal_gen
        self._reveal_ref = ref
        scroll = "center" if center else "ensure"
        kp = self._kp_for_ref(ref)
        if kp is not None and self.flat.row_of(kp) is not None:
            self._finish_reveal(gen, kp, ref, select, scroll)
            return
        src = self._src
        session = self.session
        kind = self._kind
        assert src is not None

        def work(ctx: Any) -> tuple[NodeRef, tuple, list[NodeInfo], dict]:
            r = ref
            base = src.record_base
            if r.record is None and kind == "wrapped" and len(r.path) > len(base) \
                    and all(_seg_eq(a, b) for a, b in zip(r.path, base)):
                r = session.ref_for_path(tuple(r.path), ctx.cancel)
            elif r.ipath is None or len(r.ipath) != len(r.path):
                r = session.resolve(r, ctx.cancel)
            kp2 = self._kp_for_ref(r)
            if kp2 is None:
                return r, (), [], {}
            chain = src.chain(kp2, ctx.cancel)
            return r, kp2, chain, {}

        def done(result: tuple[NodeRef, tuple, list[NodeInfo], dict]) -> None:
            if self._dead or gen != self._reveal_gen:
                return
            r, kp2, chain, _orders = result
            if not kp2:
                self._end_reveal(gen)
                return
            for info in chain:
                self._remember(info)
            top = kp2[0]
            if isinstance(top, int):
                self._want_position(top, lambda pos: self._reveal_positioned(gen, r, kp2, chain, pos, select, scroll))
            else:
                self._reveal_positioned(gen, r, kp2, chain, 0, select, scroll)

        def failed(exc: BaseException) -> None:
            if self._dead or gen != self._reveal_gen:
                return
            self._end_reveal(gen)
            text = exc.message if isinstance(exc, JsonReshapeError) else t("gui.tree.reveal_failed")
            self._notify(text)

        def cancelled() -> None:
            if not self._dead:
                self._end_reveal(gen)

        self.session.app.tasks.run(work, title=t("gui.tree.task.reveal"), kind="reveal", owner=self, silent=True,
                                   on_done=done, on_error=failed, on_cancel=cancelled)

    def _end_reveal(self, gen: int) -> None:
        """A reveal ended without reaching its node (failed, not in the view, cancelled):
        it is no longer "on its way", so a new request for the same node is not ignored."""
        if gen == self._reveal_gen:
            self._reveal_ref = None

    def _reveal_positioned(self, gen: int, ref: NodeRef, kp: tuple, chain: list[NodeInfo], pos: int | None,
                           select: bool, scroll: str) -> None:
        if self._dead or gen != self._reveal_gen:
            return
        top = kp[0]
        if isinstance(top, int):
            if pos is None:
                self._end_reveal(gen)
                self._notify(t("gui.tree.not_in_view"))
                return
            if not self.session.view_is_base:
                self._pos_cache[top] = pos
            self._sync_top()
        self._expand_chain(gen, kp, chain, lambda: self._finish_reveal(gen, kp, ref, select, scroll))

    def _expand_chain(self, gen: int, kp: tuple, chain: list[NodeInfo], done: Callable[[], Any]) -> None:
        if self._dead or gen != self._reveal_gen:
            return
        rest = list(chain)
        with self._structure():
            while rest:
                info = rest[0]
                anc = info.keypath
                if self.flat.is_expanded(anc):
                    rest.pop(0)
                    continue
                order, compute = self._order_need(anc, info.kind, info.count)
                if info.count is None or compute:
                    break
                if not self._flat_expand(anc, info.count, order, info.kind):
                    self._end_reveal(gen)
                    return
                rest.pop(0)
        if rest:
            anc = rest[0].keypath
            self.expand_node(anc, then=lambda: self._expand_chain(gen, kp, rest[1:], done),
                             otherwise=lambda: self._end_reveal(gen))
            return
        done()

    def _finish_reveal(self, gen: int, kp: tuple, ref: NodeRef, select: bool, scroll: str) -> None:
        if self._dead or gen != self._reveal_gen:
            return
        self._reveal_ref = None
        row = self.flat.row_of(kp)
        if row is None:
            self._notify(t("gui.tree.not_in_view"))
            return
        self._cur_kp = kp
        self._syncing = True
        try:
            self.grid.set_current(row, 0, select=True, scroll=scroll)
        finally:
            self._syncing = False
        self.selection_changed.emit()
        full = ref if ref.ipath is not None and len(ref.ipath) == len(ref.path) else None
        if full is None:
            info = self._info(kp)
            full = self._ref_for(kp, info) if info is not None else None
        if select:
            if full is None:
                self._select_kp(kp)            # waits for the row's data
                return
            self._pending_select = None
            self._own_ref = full
            self.session.select(full, origin=self)
            self._update_breadcrumb(full)
            return
        current = getattr(self.session, "current", None)
        if full is not None:
            self._own_ref = full
            if current is not None and same_node(current, full):
                self.session.select(full, origin=self, history=False)   # same node: adds the ipath only
        self._update_breadcrumb(full or current)

    def _notify(self, text: str) -> None:
        """A short message: the status bar when the app has one, else the view's info bar."""
        self.last_message = text
        show = getattr(self.session.app, "show_status", None)
        if callable(show):
            show(text)
        else:  # pragma: no cover - AppContext always has show_status
            self._show_message(text, "info")

    def _show_message(self, text: str, kind: str = "warning") -> None:
        self.last_message = text
        self.message_bar.set_kind(kind)
        self.message_bar.set_message(text)
        self.message_bar.show()

    # ------------------------------------------------------------------ breadcrumb
    def _crumbs(self, ref: NodeRef) -> list[tuple[str, NodeRef | None]]:
        kind = self._kind
        root_ref = NodeRef(None, (), ()) if kind in ("single", "wrapped") else None
        items: list[tuple[str, NodeRef | None]] = [("$", root_ref)]
        path = tuple(ref.path)
        ip = ref.ipath if ref.ipath is not None and len(ref.ipath) == len(path) else None
        if ref.record is None or kind == "single":
            for j, seg in enumerate(path):
                items.append((_seg_text(seg), NodeRef(None, path[:j + 1], tuple(ip[:j + 1]) if ip else None)))
            return items
        src = self._src
        base = src.record_base if src is not None else ()
        for j, seg in enumerate(base):
            items.append((_seg_text(seg), NodeRef(None, tuple(base[:j + 1]))))
        top_info = self._info((ref.record,))
        if top_info is not None:
            seg = top_info.key
        else:
            try:
                abs_path = tuple(self.session.absolute_path(ref))
                seg = abs_path[len(base)] if len(abs_path) > len(base) else ref.record
            except Exception:  # noqa: BLE001 - a closed document
                seg = ref.record
        label = display_key(seg) if isinstance(seg, str) else f"#{ref.record}"
        items.append((label, NodeRef(ref.record, (), ())))
        for j, seg in enumerate(path):
            items.append((_seg_text(seg), NodeRef(ref.record, path[:j + 1], tuple(ip[:j + 1]) if ip else None)))
        return items

    def _update_breadcrumb(self, ref: NodeRef | None) -> None:
        if ref is None:
            self._crumb_refs = []
            self.breadcrumb.set_segments([])
            return
        items = self._crumbs(ref)
        self._crumb_refs = [r for _text, r in items]
        last = len(items) - 1
        self.breadcrumb.set_segments([(text, r is not None and i != last) for i, (text, r) in enumerate(items)])

    def crumb_refs(self) -> list[NodeRef | None]:
        return list(self._crumb_refs)

    def _on_crumb(self, index: int) -> None:
        refs = self.crumb_refs()
        if not 0 <= index < len(refs) or refs[index] is None:
            return
        self.reveal(refs[index], select=True, center=False)

    def _copy_crumb_path(self) -> None:
        if self.copy_path("dotted") is not None:
            self._notify(t("gui.tree.path_copied"))

    # ------------------------------------------------------------------ keyboard / mouse
    def _on_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        mods = event.modifiers()
        K = Qt.Key
        if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                   | Qt.KeyboardModifier.MetaModifier):
            return False
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if key == K.Key_Asterisk:
            self.expand_all_current()
            return True
        if key == K.Key_Plus:
            if self._cur_kp is not None:
                self.expand_node(self._cur_kp)
            return True
        if key == K.Key_Minus:
            if self._cur_kp is not None:
                self.collapse_node(self._cur_kp)
            return True
        if shift:
            return False
        if key == K.Key_Right:
            self._key_right()
            return True
        if key == K.Key_Left:
            self._key_left()
            return True
        if key == K.Key_Space:
            self.show_full_value()
            return True
        if key == K.Key_Escape and self.find_bar.isVisible():
            self.find_bar.close_bar()
            return True
        return False

    def _current_row(self) -> int:
        return self.grid.current()[0]

    def _key_right(self) -> None:
        row = self._current_row()
        if row < 0:
            if self.flat.row_count():
                self.select_row(0)
            return
        kp = self.row_keypath(row)
        info = self._info(kp)
        if info is None or not info.expandable:
            return
        if not self.flat.is_expanded(kp):
            self.expand_node(kp)
            return
        if row + 1 < self.flat.row_count() and self.flat.parent_row(row + 1) == row:
            self.select_row(row + 1)

    def _key_left(self) -> None:
        row = self._current_row()
        if row < 0:
            return
        kp = self.row_keypath(row)
        if self.flat.is_expanded(kp):
            self.collapse_node(kp)
            return
        parent = self.flat.parent_row(row)
        if parent is not None:
            self.select_row(parent)

    def _on_activated(self, row: Any, _col: int) -> None:
        """Enter / double click: toggle (the one place stage 3 turns into "edit")."""
        if row is None or row < 0 or not self._ready():
            return
        self.toggle_row(row)

    def _on_expander(self, row: Any) -> None:
        if row is None or row < 0 or not self._ready():
            return
        self.toggle_row(row)

    def show_full_value(self) -> Any:
        """The "full value" window of the current node (Space)."""
        kp = self._cur_kp
        info = self._info(kp) if kp is not None else None
        if info is None:
            return None
        return actions.show_full_value(self, self.session, self._ref_for(kp, info))

    # ------------------------------------------------------------------ context menu
    def context_menu(self, row: int) -> QMenu | None:
        """The node context menu of ``row`` (not shown; the caller owns it)."""
        if not self._ready():
            return None
        try:
            kp = self.row_keypath(row)
        except IndexError:
            return None
        info = self._info(kp)
        if info is None:
            return None
        ref = self._ref_for(kp, info)
        owned: list[Any] = []
        extra = self._menu_extra(kp, info, owned)
        menu = actions.node_menu(self, self.session, ref, view=self, extra=extra, node=info)
        for obj in owned:
            if isinstance(obj, QMenu):
                obj.setParent(menu, obj.windowFlags())
            else:
                obj.setParent(menu)
        return menu

    def _menu_extra(self, kp: tuple, info: NodeInfo, owned: list[Any]) -> list[Any]:
        def action(text: str, name: str, slot: Callable[[], Any], *, enabled: bool = True, why: str = "",
                   menu: QMenu | None = None) -> QAction:
            act = QAction(text, menu if menu is not None else self)
            act.setObjectName(name)
            act.triggered.connect(lambda _checked=False: slot())
            if not enabled:
                act.setEnabled(False)
                act.setToolTip(why)
            if menu is not None:
                menu.addAction(act)
            else:
                owned.append(act)
            return act

        container = info.is_container and not info.records
        why = t("gui.tree.menu.records_inline") if info.records else t("gui.tree.menu.need_container")
        expand_all = action(t("gui.tree.menu.expand_all"), "tree_expand_all", lambda: self.expand_all(kp),
                            enabled=container and info.count != 0, why=why)
        expand_all.setShortcut(QKeySequence("*"))
        expand_all.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        expand_all.setShortcutVisibleInContextMenu(True)
        collapse = action(t("gui.tree.menu.collapse_all"), "tree_collapse_all", lambda: self.collapse_subtree(kp),
                          enabled=container, why=why)
        levels = QMenu(t("gui.tree.menu.expand_level"), self)
        levels.setObjectName("tree_expand_level")
        levels.setToolTipsVisible(True)
        for n in range(1, 10):
            action(t("gui.tree.menu.level", n=n), f"tree_expand_level_{n}",
                   lambda n=n: self.expand_to_level(n, kp), menu=levels)
        levels.setEnabled(container and info.count != 0)
        owned.append(levels)
        sort_menu = QMenu(t("gui.tree.menu.sort_children"), self)
        sort_menu.setObjectName("tree_sort_menu")
        sort_menu.setToolTipsVisible(True)
        keys_act = action(t("gui.tree.menu.sort_keys"), "tree_sort_keys", lambda: None, menu=sort_menu)
        keys_act.setCheckable(True)
        keys_act.setChecked(self._sort_keys)
        keys_act.setToolTip(t("gui.tree.menu.sort_keys.tip"))
        keys_act.toggled.connect(self.toggle_sort_keys)
        is_array = info.kind == "array" and container
        action(t("gui.tree.menu.sort_elements"), "tree_sort_elements", lambda: self.ask_sort_elements(kp),
               enabled=is_array and info.count != 0, why=t("gui.tree.menu.need_array"), menu=sort_menu)
        owned.append(sort_menu)
        original = action(t("gui.tree.menu.original_order"), "tree_original_order", lambda: self.original_order(kp),
                          enabled=kp in self._elem_sorts, why=t("gui.tree.menu.not_sorted"))
        return [expand_all, collapse, levels, None, sort_menu, original]

    def _on_context_menu(self, row: Any, _col: int, gpos: QPoint) -> None:
        if row is None or row < 0:
            return
        if row != self._current_row():
            self.select_row(row, scroll="none")
        menu = self.context_menu(row)
        if menu is None:
            return
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(gpos)

    def _on_header_menu(self, _index: int, gpos: QPoint) -> None:
        menu = QMenu(self)
        for i, col in enumerate(self.grid.columns()):
            if col.id == COL_KEY:
                continue
            act = menu.addAction(col.title)
            act.setCheckable(True)
            act.setChecked(col.visible)
            act.toggled.connect(lambda on, k=i: self.grid.set_column_visible(k, on))
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(gpos)

    # ------------------------------------------------------------------ expand all / levels
    def _visible_current(self) -> tuple | None:
        kp = self._cur_kp
        return kp if kp is not None and self.flat.row_of(kp) is not None else None

    def expand_all_current(self) -> None:
        """``*``: expand the whole subtree of the current node."""
        kp = self._visible_current()
        if kp is not None:
            self.expand_all(kp)

    def expand_all(self, kp: tuple) -> None:
        """Expand every container below ``kp`` (counted on the worker first; confirms
        above :data:`EXPAND_WARN_ROWS` rows, refuses above :data:`EXPAND_CAP_ROWS`)."""
        info = self._info(tuple(kp))
        if info is not None and not info.expandable:
            return
        self._run_plan([tuple(kp)], None, tuple(kp))

    def expand_to_level(self, n: int, kp: tuple | None = None) -> None:
        """Expand ``n`` (1–9) levels below the current node (or ``kp``; the top level when
        there is none); deeper expansions below it are collapsed."""
        n = max(1, min(9, int(n)))
        if kp is None:
            kp = self._visible_current()
        if kp is None:
            self._run_plan(None, n, None)
        else:
            self._run_plan([tuple(kp)], n, tuple(kp))

    def collapse_all(self) -> None:
        """Collapse every node of the tree (remembered expansions are forgotten)."""
        if not self._ready():
            return
        with self._structure():
            self.flat.collapse_all()
            self._kinds.clear()

    def collapse_subtree(self, kp: tuple) -> None:
        """Collapse ``kp`` and forget the expansions below it."""
        kp = tuple(kp)
        n = len(kp)
        below = [e for e in self.flat.expanded_keypaths() if e[:n] == kp]
        if not below:
            return
        with self._structure():
            for e in sorted(below, key=len, reverse=True):
                self.flat.collapse(e)
                self._kinds.pop(e, None)

    def _run_plan(self, roots: list[tuple] | None, levels: int | None, root: tuple | None) -> None:
        if not self._ready():
            return
        if self._plan_task is not None:
            self._plan_task.cancel()
        self._plan_gen += 1            # an older plan (or its open question) is void now
        self._close_plan_box()
        plan_gen = self._plan_gen
        src = self._src
        assert src is not None
        doc = self._doc
        sort_keys = self._sort_keys
        orders = {k: v for k, v in self._orders.items()}
        elems = dict(self._elem_sorts)
        positions: dict[Any, int] = {}
        view_gen = self._view_gen
        if roots is None:
            count, key_fn, _pos_fn = self._top_spec()
            off = self._offset()
            base = self.session.view_is_base

            def top_roots() -> Iterator[tuple]:
                for p in range(count):
                    key = key_fn(p) if key_fn is not None else p
                    if isinstance(key, int) and not base:
                        positions[key] = p - off
                    yield (key,)

            root_iter: Any = top_roots
            label = t("gui.tree.top_level")
        else:
            root_iter = lambda: iter(roots)  # noqa: E731
            info = self._info(roots[0])
            label = _path_label(info.path) if info is not None else ""

        def order_for(kp: tuple, node: Any, count: int, cancel: Any) -> Any:
            cached = orders.get(kp)
            if cached is not None and len(cached[1]) == count:
                return cached
            if kp in elems:
                field, desc = elems[kp]
                order = src.element_order(node, count, SortKey(field, descending=desc), cancel)
                return "elements", DisplayOrder(order, count)
            if sort_keys and node.kind == "object" and count > 1:
                return "keys", DisplayOrder(src.key_order(node, count, cancel), count)
            return None

        def work(ctx: Any) -> ExpandPlan:
            plan = src.plan(root_iter(), levels=levels, cap=EXPAND_CAP_ROWS, order_for=order_for,
                            cancel=ctx.cancel, progress=ctx.progress)
            plan.positions = dict(positions)
            return plan

        def done(plan: ExpandPlan) -> None:
            if task is self._plan_task:
                self._plan_task = None
            if self._dead or self._doc is not doc or plan_gen != self._plan_gen:
                return
            if view_gen != self._view_gen:
                plan.positions = {}
            self._apply_plan(plan, levels, root, view_gen)

        def finished_other(*_args: Any) -> None:
            if task is self._plan_task:
                self._plan_task = None

        def failed(exc: BaseException) -> None:
            finished_other()
            if not self._dead:
                widgets.report_task_error(self.session, exc, self, purpose="view")

        task = self.session.app.tasks.run(work, title=t("gui.tree.task.expand", path=label), kind="children",
                                          owner=self, on_done=done, on_error=failed, on_cancel=finished_other)
        self._plan_task = task

    def _apply_plan(self, plan: ExpandPlan, levels: int | None, root: tuple | None,
                    view_gen: int | None = None) -> None:
        """Apply a finished plan (runs in a task callback: never opens a modal loop here;
        a big plan is confirmed through :meth:`_ask_plan` and applied after the answer)."""
        if plan.overflow:
            self._show_message(t("gui.tree.too_many", rows=widgets.format_count(EXPAND_CAP_ROWS)))
            return
        if plan.rows > EXPAND_WARN_ROWS:
            self._ask_plan(plan, levels, root, self._view_gen if view_gen is None else view_gen)
            return
        self._expand_plan(plan, levels, root)

    def _ask_plan(self, plan: ExpandPlan, levels: int | None, root: tuple | None, view_gen: int) -> None:
        """"Expand N rows?" without a nested event loop: a window-modal box opened with
        ``open()`` (or the test hook :data:`widgets.confirm_handler`, called from a zero
        timer); the plan is applied on "yes" when nothing replaced it meanwhile."""
        self._close_plan_box()
        gen = self._plan_gen
        doc = self._doc
        text = t("gui.tree.expand_confirm", rows=widgets.format_count(plan.rows))
        title = t("gui.tree.expand_confirm.title")

        def answer(yes: bool) -> None:
            if not yes or self._dead or gen != self._plan_gen or self._doc is not doc or not self._ready():
                return
            if view_gen != self._view_gen:
                plan.positions = {}     # positions of another view
            self._expand_plan(plan, levels, root)

        handler = widgets.confirm_handler
        if handler is not None:
            self._plan_auto = True

            def auto() -> None:
                self._plan_auto = False
                if not self._dead and gen == self._plan_gen:
                    answer(bool(handler(self, text, title)))

            QTimer.singleShot(0, self, auto)
            return
        box = QMessageBox(QMessageBox.Icon.Question, title, text, QMessageBox.StandardButton.NoButton, self)
        box.setObjectName("treeExpandConfirm")
        box.setTextFormat(Qt.TextFormat.PlainText)
        yes = box.addButton(t("gui.common.yes"), QMessageBox.ButtonRole.YesRole)
        no = box.addButton(t("gui.common.no"), QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        def finished(_result: int) -> None:
            if self._plan_box is box:
                self._plan_box = None
            answer(box.clickedButton() is yes)

        box.finished.connect(finished)
        self._plan_box = box
        box.open()

    def _close_plan_box(self) -> None:
        """Close an open "expand N rows?" question (its plan is void: the caller bumped
        ``_plan_gen``, or the view is going away)."""
        box = self._plan_box
        self._plan_box = None
        if box is not None:
            with contextlib.suppress(RuntimeError):
                box.close()

    def plan_question(self) -> QMessageBox | None:
        """The open "expand N rows?" box (``None`` when there is none; tests)."""
        return self._plan_box

    def _expand_plan(self, plan: ExpandPlan, levels: int | None, root: tuple | None) -> None:
        with self._structure():
            if levels is not None:
                if root is None:
                    self.flat.collapse_all()
                    self._kinds.clear()
                else:
                    n = len(root)
                    deep = [e for e in self.flat.expanded_keypaths() if e[:n] == root and len(e) - n >= levels]
                    for e in sorted(deep, key=len, reverse=True):
                        self.flat.collapse(e)
                        self._kinds.pop(e, None)
            if plan.positions:
                for key, pos in plan.positions.items():
                    self._pos_cache[key] = pos
                    self._pos_asked.add(key)
                self._sync_top()
            for kp, count, order, kind in plan.items:
                arr = None
                if order is not None:
                    mode, arr = order
                    self._store_order(kp, mode, arr)
                self._flat_expand(kp, count, arr, kind)

    # ------------------------------------------------------------------ node sorting
    def sort_keys_enabled(self) -> bool:
        return self._sort_keys

    def toggle_sort_keys(self, on: bool | None = None) -> None:
        """Show the members of every object sorted by key (Uzbek collation, natural digit
        runs; view only) -- or in file order again."""
        on = (not self._sort_keys) if on is None else bool(on)
        if on == self._sort_keys:
            return
        self._sort_keys = on
        if not self._ready():
            return
        objects = [kp for kp in self.flat.expanded_keypaths()
                   if self._kinds.get(kp) == "object" and kp not in self._elem_sorts]
        if not on:
            with self._structure():
                for kp in objects:
                    self._drop_order(kp)
                    with contextlib.suppress(KeyError, ValueError):
                        self.flat.set_order(kp, None)
            return
        todo = []
        with self._structure():
            for kp in objects:
                cached = self._orders.get(kp)
                if cached is not None and cached[0] == "keys":
                    with contextlib.suppress(KeyError, ValueError):
                        self.flat.set_order(kp, cached[1])
                else:
                    todo.append(kp)
        if not todo:
            return
        src = self._src
        assert src is not None
        doc = self._doc

        def work(ctx: Any) -> None:
            for kp in todo:
                check(ctx.cancel)
                node = src.node(kp, ctx.cancel)
                count = int(node.child_count(cancel=ctx.cancel))
                if count > 1:
                    ctx.post((kp, DisplayOrder(src.key_order(node, count, ctx.cancel), count)))

        def item(result: tuple[tuple, DisplayOrder]) -> None:
            if self._dead or self._doc is not doc or not self._sort_keys:
                return
            kp, order = result
            if kp in self._elem_sorts:
                return
            self._store_order(kp, "keys", order)
            if self.flat.is_expanded(kp):
                with self._structure(), contextlib.suppress(KeyError, ValueError):
                    self.flat.set_order(kp, order)

        self.session.app.tasks.run(work, title=t("gui.tree.task.sort_keys", path=t("gui.tree.all_objects")),
                                   kind="children", owner=self, on_item=item,
                                   on_error=lambda exc: widgets.report_task_error(self.session, exc, self,
                                                                                  purpose="view"))

    def ask_sort_elements(self, kp: tuple) -> None:
        """"Elementlarni maydon bo'yicha tartiblash…": read the field names of the first
        elements (worker), then ask with :class:`ElementSortDialog`."""
        if not self._ready():
            return
        kp = tuple(kp)
        src = self._src
        assert src is not None
        info = self._info(kp)
        path = _path_label(info.path) if info is not None else ""

        def show(fields: list[str]) -> None:
            if self._dead:
                return
            dlg = ElementSortDialog(fields, self, path=path, current=self._elem_sorts.get(kp))
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self._sort_dialog = dlg
            dlg.accepted.connect(lambda: self._sort_dialog_accepted(kp, dlg))
            dlg.finished.connect(lambda _r: setattr(self, "_sort_dialog", None))
            dlg.open()

        self.session.app.tasks.run(lambda ctx: src.field_candidates(kp, ctx.cancel), title=t("gui.tree.task.fields"),
                                   kind="value", owner=self, silent=True, on_done=show,
                                   on_error=lambda exc: widgets.report_task_error(self.session, exc, self,
                                                                                  purpose="view"))

    def _sort_dialog_accepted(self, kp: tuple, dlg: ElementSortDialog) -> None:
        field = dlg.field()
        if field:
            self.sort_elements(kp, field, dlg.descending())

    def sort_elements(self, kp: tuple, field: str, descending: bool = False) -> bool:
        """Show the elements of the array ``kp`` sorted by ``field`` (a path inside each
        element, ``$`` = the element; nulls and missing values last; stable). The shown
        indices stay the file indices. False when ``field`` is invalid (reported).

        The order is computed by a task (visible above :data:`BIG_TASK_CHILDREN` elements)
        reporting elements done / total; cancelling it (or a failure) keeps the order shown
        before. Asked while the node's children / sort task runs, it sorts after that one."""
        kp = tuple(kp)
        try:
            SortKey(field, descending=bool(descending))
        except JsonReshapeError as exc:
            self.session.app.report_error(exc, self)
            return False
        if kp in self._busy:              # its children / sort task still runs: sort after it
            again = lambda: self.sort_elements(kp, field, descending)  # noqa: E731
            self._busy[kp].append((again, again))
            return True
        prev_spec = self._elem_sorts.get(kp)
        prev_order = self._orders.get(kp)
        self._elem_sorts[kp] = (field, bool(descending))
        self._drop_order(kp)
        if not self.flat.is_expanded(kp):
            self.expand_node(kp)
            return True
        info = self._info(kp)
        if info is None:
            return True
        src = self._src
        assert src is not None
        doc = self._doc
        spec_field, spec_desc = field, bool(descending)
        mine = (spec_field, spec_desc)

        def work(ctx: Any) -> DisplayOrder:
            node = src.node(kp, ctx.cancel)
            count = int(node.child_count(cancel=ctx.cancel))
            _count_progress(ctx.progress, count, t("gui.tree.task.sorting"))
            order = src.element_order(node, count, SortKey(spec_field, descending=spec_desc), ctx.cancel,
                                      ctx.progress)
            return DisplayOrder(order, count)       # inverted here, not on the GUI thread

        def waiting() -> _Callbacks:
            """Expands asked for while the sort ran (the node was collapsed meanwhile)."""
            return self._busy.pop(kp, [])

        def resume(callbacks: _Callbacks) -> None:
            if callbacks:
                self.expand_node(kp, then=_chain(callbacks), otherwise=_chain(callbacks, failed=True))

        def done(order: DisplayOrder) -> None:
            callbacks = waiting()
            if self._dead or self._doc is not doc:
                return
            if self._elem_sorts.get(kp) == mine:
                self._store_order(kp, "elements", order)
                if self.flat.is_expanded(kp):
                    with self._structure(), contextlib.suppress(KeyError, ValueError):
                        self.flat.set_order(kp, order)
            self.grid.refresh()
            resume(callbacks)

        def stop(exc: BaseException | None = None) -> None:
            """Cancelled or failed: the node keeps showing its previous order, so the view
            forgets this sort (the "original order" menu item stays true)."""
            callbacks = waiting()
            if self._dead or self._doc is not doc:
                return
            if self._elem_sorts.get(kp) == mine:
                if prev_spec is None:
                    self._elem_sorts.pop(kp, None)
                else:
                    self._elem_sorts[kp] = prev_spec
                if prev_order is not None and kp not in self._orders:
                    self._store_order(kp, *prev_order)
            self.grid.refresh()
            if exc is not None:
                widgets.report_task_error(self.session, exc, self, purpose="view")
            resume(callbacks)

        self._busy[kp] = []
        self.session.app.tasks.run(work, title=t("gui.tree.task.sort_elements", path=_path_label(info.path)),
                                   kind="children", owner=self, silent=(info.count or 0) <= BIG_TASK_CHILDREN,
                                   unit="items", on_done=done, on_error=stop, on_cancel=stop)
        self.grid.refresh()
        return True

    def original_order(self, kp: tuple) -> None:
        """Back to the file order of an array sorted by a field."""
        kp = tuple(kp)
        if self._elem_sorts.pop(kp, None) is None:
            return
        self._drop_order(kp)
        if self.flat.is_expanded(kp):
            with self._structure(), contextlib.suppress(KeyError, ValueError):
                self.flat.set_order(kp, None)

    def element_sort(self, kp: tuple) -> tuple[str, bool] | None:
        return self._elem_sorts.get(tuple(kp))

    # ------------------------------------------------------------------ quick find
    def find_next(self, backwards: bool = False) -> None:
        """F3 / Shift+F3: the next / previous match (wrapping); opens the bar when there
        is no search yet."""
        if not self.find_bar.isVisible() or not self.find_bar.text():
            self.find_bar.open_bar()
            return
        if not self._qf_matches:
            self._qf_status(None)
            return
        rows = self._qf_rows()
        if not rows:
            self._qf_status(None)
            return
        cur = self._current_row()
        if backwards:
            before = [r for r in rows if r[0] < cur]
            target = before[-1] if before else rows[-1]
        else:
            after = [r for r in rows if r[0] > cur]
            target = after[0] if after else rows[0]
        self._qf_goto(target[1], rows)

    def _qf_rows(self) -> list[tuple[int, tuple]]:
        out = []
        with self._peeking():
            for kp in self._qf_matches:
                row = self.flat.row_of(kp)
                if row is not None:
                    out.append((row, kp))
        out.sort()
        return out

    def _qf_goto(self, kp: tuple, rows: list[tuple[int, tuple]], *, history: bool = True) -> None:
        self._qf_current = kp
        self._set_current_kp(kp, scroll="center", history=history)
        index = next((i for i, (_r, k) in enumerate(rows) if k == kp), None)
        self._qf_status(index, len(rows))
        self.grid.refresh()

    def _qf_status(self, index: int | None, total: int | None = None) -> None:
        total = len(self._qf_matches) if total is None else total
        state = "found" if total else "notfound"
        if not self.find_bar.text():
            self.find_bar.set_status("")
            return
        self.find_bar.set_status(quickfind.status_text(index, total, capped=self._qf_capped), state)

    def quick_find_matches(self) -> dict[tuple, tuple[tuple, tuple]]:
        return dict(self._qf_matches)

    def _qf_closed(self) -> None:
        self._qf_clear()
        self.grid.setFocus(Qt.FocusReason.OtherFocusReason)

    def _qf_clear(self) -> None:
        self._qf_gen += 1
        if self._qf_task is not None:
            self._qf_task.cancel()
            self._qf_task = None
        self._qf_matches = {}
        self._qf_current = None
        self._qf_capped = False
        self.grid.refresh()

    def _qf_rescan(self) -> None:
        if self.find_bar.isVisible() and self.find_bar.text():
            self._qf_search(self.find_bar.text(), self.find_bar.case_sensitive(), jump=False)

    def _qf_entries(self) -> tuple[list[tuple[tuple, NodeInfo | None]], bool]:
        """Rows quick find looks at (what is on screen is always among them):

        * the rows in the viewport;
        * every row below a visible expanded node (at most :data:`QUICK_SCAN_ROWS` rows:
          ``capped`` when that budget ran out);
        * the top rows whose data is cached (records fetched lately and still in the view).

        Unexpanded records that were never shown are not loaded. Rows whose data is not
        cached come with ``None`` (the worker fetches them). No position lookup starts."""
        flat = self.flat
        out: list[tuple[tuple, NodeInfo | None]] = []
        seen: set[tuple] = set()
        capped = False

        def add(ri: RowInfo, *, load_tops: bool) -> None:
            kp = ri.keypath
            if kp in seen:
                return
            info = self._info(kp)
            if ri.depth == 0 and info is None and not load_tops:
                return
            seen.add(kp)
            out.append((kp, info))

        with self._peeking():
            first, last = self.grid.visible_rows()
            if last >= first:
                for ri in flat.iter_rows(first, last + 1):
                    add(ri, load_tops=True)
            starts = []
            for kp in flat.expanded_keypaths():
                row = flat.row_of(kp)
                if row is not None:
                    starts.append(row)
            starts.sort()
            covered = -1
            budget = QUICK_SCAN_ROWS
            for row in starts:
                if row <= covered:
                    continue
                if budget <= 0:
                    capped = True
                    break
                last = flat.last_descendant_row(row)
                stop = min(last + 1, row + budget)
                for ri in flat.iter_rows(row, stop):
                    add(ri, load_tops=False)
                budget -= stop - row
                covered = last
                if stop <= last:
                    capped = True
                    break
            src = self._src
            if src is not None:
                for kp in list(src.recent_tops):
                    if kp in seen:
                        continue
                    info = self._info(kp)
                    if info is None or flat.row_of(kp) is None:
                        continue
                    seen.add(kp)
                    out.append((kp, info))
        return out, capped

    def _qf_search(self, text: str, case_sensitive: bool, jump: bool = True) -> None:
        self._qf_gen += 1
        gen = self._qf_gen
        if self._qf_task is not None:
            self._qf_task.cancel()
            self._qf_task = None
        matcher = quickfind.make_matcher(text, case_sensitive)
        if matcher is None or not self._ready():
            self._qf_matches = {}
            self._qf_current = None
            self._qf_capped = False
            self._qf_status(None, 0)
            self.grid.refresh()
            return
        entries, capped = self._qf_entries()
        src = self._src
        assert src is not None

        def work(ctx: Any) -> dict[tuple, tuple[tuple, tuple]]:
            missing = [kp for kp, info in entries if info is None]
            fetched: dict[tuple, Any] = {}
            for a in range(0, len(missing), 256):
                check(ctx.cancel)
                fetched.update(src.fetch(missing[a:a + 256], ctx.cancel))
            texts = []
            for kp, info in entries:
                inf = info if info is not None else fetched.get(kp)
                if isinstance(inf, NodeInfo):
                    texts.append((kp, *search_texts(kp, inf)))
            return find_matches(texts, matcher, limit=QUICK_MAX_MATCHES, cancel=ctx.cancel)

        def done(matches: dict[tuple, tuple[tuple, tuple]]) -> None:
            if self._dead or gen != self._qf_gen:
                return
            self._qf_task = None
            self._qf_matches = matches
            self._qf_capped = capped or len(matches) >= QUICK_MAX_MATCHES
            rows = self._qf_rows()
            if not rows:
                self._qf_current = None
                self._qf_status(None, 0)
                self.grid.refresh()
                return
            if jump:
                cur = self._current_row()
                after = [r for r in rows if r[0] >= cur]
                self._qf_goto((after[0] if after else rows[0])[1], rows, history=False)
            else:
                if self._qf_current not in matches:
                    self._qf_current = None
                index = next((i for i, (_r, k) in enumerate(rows) if k == self._qf_current), None)
                self._qf_status(index, len(rows))
                self.grid.refresh()

        self._qf_task = self.session.app.tasks.run(work, title=t("gui.tree.task.find"), kind="find", owner=self,
                                                   silent=True, on_done=done)


# ==================================================================================
# helpers
# ==================================================================================
def _spans(spans: tuple, current: bool) -> tuple:
    if not current:
        return tuple(spans)
    return tuple((s, e, True) for s, e in spans)


def _call(fn: Callable[[], Any] | None) -> None:
    if fn is not None:
        try:
            fn()
        except Exception:  # pragma: no cover - defensive
            log.exception("tree callback failed")


def _chain(callbacks: _Callbacks, *, failed: bool = False) -> Callable[[], None]:
    """A function calling every ``then`` (or, ``failed``, every ``otherwise``) of
    ``(then, otherwise)`` pairs."""
    k = 1 if failed else 0

    def run() -> None:
        for pair in callbacks:
            _call(pair[k])

    return run


def _count_progress(progress: Any, total: int, message: str) -> None:
    """Switch a task's progress to counting ``total`` items (node sorting after the child
    count: the tasks panel then shows elements done / total, rate and time left)."""
    progress.unit = "items"
    progress.restart_clock()
    progress.set_total(max(1, int(total)))
    progress.update(0)
    progress.set_message(message)


def _set_count(info: NodeInfo, count: int) -> None:
    """A container's child count became known: also fix the ``{?}`` badge of its value
    text (and ``{}`` / ``[]`` when it is empty)."""
    unknown = count_badge(info.kind, None)
    info.count = count
    if info.is_container and info.text.startswith(unknown):
        if count == 0:
            info.text = "{}" if info.kind == "object" else "[]"
        else:
            info.text = count_badge(info.kind, count) + info.text[len(unknown):]


def _seg_eq(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if type(a) is type(b):
        return a == b
    from jsonreshape.paths import canonical_index

    if isinstance(a, str) and isinstance(b, int):
        return canonical_index(a) == b
    if isinstance(a, int) and isinstance(b, str):
        return canonical_index(b) == a
    return False


