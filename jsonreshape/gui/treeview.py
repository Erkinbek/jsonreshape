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

Editing (docs/ARCHITECTURE.md §12.9)
====================================
* **Selection.** The grid is in ``"rows"`` mode. The view records the selected rows as key paths
  (up to :data:`EDIT_SELECTION_MAX` rows; "everything" -- Ctrl+A -- stays everything) right before
  every structure change and selects them again afterwards (nodes hidden by a collapse leave the
  selection; a bigger selection cannot be followed and shrinks to the current row); data changes
  re-key them. :meth:`TreeView.selected_keypaths` is the normalized selection of the node commands
  (no wrapper / root / record-container rows, no descendants of selected nodes);
  :meth:`TreeView.selection_count` counts selected rows.
* **Keys** (``_on_key``, before the Ctrl/Alt filter; never window shortcuts): Return edit,
  Shift+Return edit as JSON, F2 rename, Insert insert menu (child / before / after; record rows: a new
  record), Del delete, Ctrl+D duplicate, Ctrl+V (also Shift+Ins) paste, Alt+Up/Down move. Decision:
  while editing is not available Return still toggles a container (the stage-2 behaviour) and reports
  the reason for scalars; Return also toggles the wrapper object (never edited as a whole). Del,
  Ctrl+D and Alt+arrows on protected rows (the wrapper, its record container, a single document's
  root) say why they do nothing. Double click: containers toggle, scalars edit.
* **Values.** :meth:`~jsonreshape.gui.treemodel.NodeSource.edit_payload` reads the kind and exact
  token (scalars up to :data:`TOKEN_MAX`) in a silent task; when it arrives and the node is still
  current: :class:`~jsonreshape.gui.editors.InlineEditor` over the value cell for strings up to
  :data:`INLINE_MAX_CHARS` characters without a line break and for numbers (exact token),
  :class:`~jsonreshape.gui.editors.StringDialog` for other strings (above 64 KiB through its own
  loader), bools toggle at once, null / containers / Shift+Enter open
  :meth:`JsonEditDialog.for_node <jsonreshape.gui.editors.JsonEditDialog.for_node>`. F2 edits the
  key in the key cell (:class:`~jsonreshape.gui.editors.KeyDialog` for long keys). An open dialog
  follows its node through edits applied meanwhile (``_Pending``: the ref is remapped with every
  ChangeSet; a node deleted meanwhile is reported when the dialog is accepted -- review3 L2); every
  applied change cancels an open inline editor. Key editors (rename, insert) refuse keys that exist
  in place: the loaded siblings at once (``taken``), the others -- and record keys -- looked up in
  a silent task when the key is committed (``key_check``, ``Node.find_child_index``; review3 L1).
* **Commands** go to ``session.edit`` (:class:`~jsonreshape.gui.editing.EditController`) with
  NodeRefs; rows whose data is not loaded are read first (silent task). Selections above
  :data:`EDIT_SELECTION_MAX` rows that hold record rows only use ``delete_records`` /
  ``duplicate_records`` (base view: lazy position ranges; other views: positions read from the view's
  record set in a task); with nested rows they are refused (message: use Bulk operations).
  Alt+arrows and drags are refused for children of a parent shown in a display order and for record
  rows outside the base view. The edit protocol of :mod:`jsonreshape.gui.actions` (node menu, Edit
  menu): ``edit_value``, ``edit_as_json``, ``rename_key``, ``insert_node(where)``,
  ``duplicate_selection``, ``delete_selection``, ``move_selection(delta)``, ``change_type(kind)``,
  ``paste_json``, ``edit_command_state(name)``; ``bulk_selection()`` for the bulk dialog.
* **Drag & drop** through ``VirtualGrid.set_row_drag``: before / inside / after; "after" an open
  container's row means before its first child; refused: into the own subtree, inside scalars and
  the wrapper's record container, the wrapper / root rows, record rows outside the base view, between
  children of a sorted parent, no editing.
* **Applying a ChangeSet** (``session.data_changed`` -> :meth:`TreeView._apply_change`): the view's
  tasks are detached (callbacks waiting for them are told "failed"); a new
  :class:`~jsonreshape.gui.treemodel.NodeSource` pinned to the new state is installed BEFORE the
  loader forgets (a fetch starting in between reads the new state; results of an older source are
  dropped); the loaded infos of unchanged nodes around the window are kept at their new key paths
  (array-like records get their new position in path / key) so an edit does not blank the window;
  one ``_rekey`` moves every key-path keyed map; the FlatTree gets the record remap, then every
  NodeChange in order (record ``None`` -> wrapper, single documents -> root); the first surviving node
  of the screen stays where it was; the row of ``cs.select`` becomes current (a node of the re-keyed
  selection keeps the selection, so a block moved with Alt+arrows stays selected; a node under collapsed
  ancestors is revealed quietly) -- the session selects that node itself once every view processed the
  ChangeSet (the tree never calls ``session.select`` from its ``data_changed`` slot: views after it
  would get a ref of the new state before their own ``data_changed``); expanded nodes whose children
  or values changed get their kind and display order computed again in one task (until then a node
  whose children changed shows file order). A reset ChangeSet (restore / discard) rebuilds the view.
  A state installed without a ChangeSet (editing became possible) is picked up by ``_sync_source``.
* Edited / new nodes (a span in the edit layer, :func:`jsonreshape.overlay.is_edit_offset`) show
  "tahrirlangan" instead of a byte offset (offset column, size tooltip).

Smart values (docs/ARCHITECTURE.md §14.7, §14.9)
================================================
The value column shows the smart value the loader worker found in the text it read anyway
(:attr:`NodeInfo.smart`, :func:`~jsonreshape.gui.treemodel.smart_value`; a cut string counts
only as an image): unix times and ISO dates get a tooltip in Tashkent time (UTC+05:00, Uzbek
weekday, the pre-1992 note), ``http(s)`` / ``mailto`` links are drawn underlined in the theme's
link colour (between the quotes) and a Ctrl+click asks "Brauzerda ochilsinmi?" before
:func:`jsonreshape.gui.widgets.open_url` opens them, ``#rgb`` / ``#rrggbb`` / ``#rrggbbaa``
colours get a swatch, base64 images a tooltip with their size (the thumbnail is in the
full-value window). Nothing is detected in the GUI thread and nothing extra is read.

"Tanlangan ikkitasini taqqoslash" (node menu): enabled when exactly two nodes are selected
(:meth:`TreeView.compare_refs`); opens a compare window
(:func:`jsonreshape.gui.comparewindow.compare_selected`, ``reveal`` = the window's
``show_node`` when it has one).
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import re
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterator
from itertools import chain
from typing import Any

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QKeyEvent, QKeySequence, QPalette
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
    smart_cell,
)
from jsonreshape.gui.vgrid import Cell, Column, VirtualGrid, drop_zone
from jsonreshape.i18n import t
from jsonreshape.overlay import is_edit_offset
from jsonreshape.sortkeys import SortKey
from jsonreshape.tasks import check

log = logging.getLogger(__name__)

__all__ = ["Breadcrumb", "ElementSortDialog", "TreeView", "EDIT_SELECTION_MAX", "EXPAND_WARN_ROWS",
           "EXPAND_CAP_ROWS", "QUICK_SCAN_ROWS"]

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
# editing (§12.9)
EDIT_SELECTION_MAX = 10_000    # node commands take at most this many nodes (bigger: record commands / bulk)
INLINE_MAX_CHARS = 200         # a string up to this long without a line break is edited in the cell
TOKEN_MAX = 64 << 10           # scalar tokens read for the inline editor (bigger strings: the dialog loader)
CARRY_ROWS = 4096              # an edit keeps the loaded rows of unchanged nodes around the window (no flicker)
LOADED_KEYS_MAX = 4096         # key editors compare at once with at most this many loaded siblings (then: key check)

_Callbacks = list[tuple[Callable[[], Any] | None, Callable[[], Any] | None]]   # (then, otherwise)
_RECORD_KEYS = object()        # a key editor's target: the record keys of a member-record document

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


class _TreeDrag:
    """The row drag handler of the tree's grid (``VirtualGrid.set_row_drag``): delegates to
    the view (weakly referenced: the grid keeps the handler, the view keeps the grid)."""

    def __init__(self, view: TreeView) -> None:
        self._view = weakref.ref(view)

    def can_start(self, rows: list[tuple[int, int]]) -> bool:
        view = self._view()
        return view is not None and not view._dead and view._drag_can_start(rows)

    def target(self, row: int, frac: float) -> tuple[int, str] | None:
        view = self._view()
        return None if view is None or view._dead else view._drag_target(row, frac)

    def drop(self, rows: list[tuple[int, int]], row: int, zone: str) -> None:
        view = self._view()
        if view is not None and not view._dead:
            view._drag_drop(rows, row, zone)


# ==================================================================================
# the view
# ==================================================================================
class _Pending:
    """An open editor (inline editor / dialog) of the tree and the node it acts on: remapped by every
    applied ChangeSet; ``None`` once the node was deleted (or the document reset / reopened)."""

    __slots__ = ("editor", "ref")

    def __init__(self, ref: NodeRef | None) -> None:
        self.ref = ref
        self.editor: Any = None


class _LoadedKeys:
    """``key in taken`` for the key editors: a member with that key is loaded in the tree now in the
    object ``target()`` names (a key path; the record level and unknown targets hold nothing here).
    Members not loaded yet are left to the editors' key check."""

    __slots__ = ("target", "view")

    def __init__(self, view: TreeView, target: Callable[[], Any]) -> None:
        self.view = view
        self.target = target

    def __contains__(self, key: object) -> bool:
        where = self.target()
        view = self.view
        if not isinstance(where, tuple) or not isinstance(key, str) or view._dead:
            return False
        n = view.flat.child_count(where) or 0
        for i in range(min(n, LOADED_KEYS_MAX)):
            info = view._peek_info(where + (i,))
            if info is not None and info.key == key:
                return True
        return False


def _insert_place(anchor: NodeRef, where: str) -> tuple[NodeRef | None, int | None]:
    """``(parent, index)`` of an insert ``where`` (child / before / after) the node ``anchor``: a
    child goes at the end of ``anchor``; a sibling of a record goes to the record level (parent
    ``None``, index = position)."""
    if where == "child":
        return anchor, None
    step = 1 if where == "after" else 0
    ipath = tuple(anchor.ipath) if anchor.ipath is not None else ()
    if not ipath:
        return None, int(anchor.record) + step
    return NodeRef(anchor.record, tuple(anchor.path)[:-1], ipath[:-1]), int(ipath[-1]) + step


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
        self._reveal_quiet = False                 # the reveal on its way reports no "not in view"
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
        # editing (§12.9)
        self._data_gen = 0                         # bumped by every applied ChangeSet (NodeSource.gen)
        self._applied_gen = 0                      # cs.gen of the last ChangeSet applied here
        self._sel_kps: list[tuple] | None = []     # selected key paths (row order); None: too many rows
        self._sel_all = False                      # everything is selected (Ctrl+A): re-applied as such
        self._sel_all_asked = False                # the last selection was made by select_all()
        self._selecting_all = False
        self._sel_dirty = False                    # the grid selection changed since the last snapshot
        self._editor: Any = None                   # the open inline editor
        self._editor_kp: tuple | None = None
        self._load_gen = 0                         # editor value loads: only the newest one opens
        self._drag = _TreeDrag(self)
        self._drag_nodes: list[tuple] = []         # normalized nodes of the running row drag
        self._pending_command: tuple[tuple, Callable[[], Any]] | None = None   # waits for the row's data
        self._pendings: list[_Pending] = []        # open editors and their (remapped) nodes

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
        self.grid.set_selection_mode("rows")
        self.grid.set_font(QFont(self._data_font))
        self.grid.set_placeholder_text(t("gui.tree.placeholder.opening"))
        self.grid.set_key_handler(self._on_key)
        self.grid.set_row_drag(self._drag)
        self.grid.current_changed.connect(self._on_grid_current)
        self.grid.selection_changed.connect(self._on_grid_selection)
        self.grid.activated.connect(self._on_activated)
        self.grid.expander_clicked.connect(self._on_expander)
        self.grid.context_menu_requested.connect(self._on_context_menu)
        self.grid.header_context_menu.connect(self._on_header_menu)
        self.grid.columns_changed.connect(self._on_columns_changed)
        self.grid.link_activated.connect(self._on_link)
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
        data_changed = getattr(s, "data_changed", None)          # editing (§12.9)
        if data_changed is not None:
            self._conns.append((data_changed, self._on_data_changed))
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
        """Number of selected rows (the status bar shows it when it is above 1)."""
        return self.grid.selected_row_count()

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
        self._close_editor()
        self.grid.set_row_drag(None)
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
        self._data_gen += 1
        self._applied_gen = int(getattr(self.session, "data_gen", 0) or 0)
        self._src = self._make_source(doc) if doc is not None else None
        self._kind = doc.kind if doc is not None else ""
        # the old key paths mean nothing for the new document: forget them, the window too
        # (invalidate() would fetch the old window again -- from a document that may still be
        # indexing, and the failures would be cached; review3 H1)
        self._loader.clear()
        self._close_editor()
        self._follow_pendings(None)            # open dialogs: their nodes are gone
        self._load_gen += 1
        self.flat = FlatTree()
        self._row_cache.clear()
        self._sel_kps, self._sel_all, self._sel_dirty, self._sel_all_asked = [], False, False, False
        self._pending_command = None
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

    def _make_source(self, doc: Any) -> NodeSource:
        """A NodeSource reading ONE state of ``doc`` (a pinned view: an edit applied while a
        fetch runs is never mixed into it)."""
        try:
            view = doc.pinned()
        except Exception:  # noqa: BLE001 - a closed / partial document: read it as it is
            view = doc
        return NodeSource(view, gen=self._data_gen)

    def _sync_source(self) -> None:
        """The document shows another overlay state than the NodeSource reads without a
        ChangeSet reaching this view (e.g. editing became possible: the edit session's
        initial state): read that state from now on; when either state has edits the
        loaded rows are fetched again (the key paths are the same: no ChangeSet means no
        structure change the view could follow)."""
        src, doc = self._src, self._doc
        if src is None or doc is None:
            return
        try:
            gen = doc.gen
            pinned = src.doc.gen
        except Exception:  # noqa: BLE001 - a closed document
            return
        if gen == pinned:
            return
        edited = bool(getattr(doc, "edited", False) or getattr(src.doc, "edited", False))
        self._src = self._make_source(doc)
        self._src.recent_tops.extend(src.recent_tops)
        if edited:
            self._data_gen += 1
            self._src.gen = self._data_gen
            self._known.clear()
            self._loader.invalidate()

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
        if self._dead:
            return
        if self._doc is self.session.doc:
            self._loader.invalidate_failed()     # rows that failed while the index was growing
        if not self._active:
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
        if self._reveal_ref is not None and same_node(ref, self._reveal_ref):
            return   # already on its way (e.g. the node an edit selected)
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
        now = self._src
        if now is None or now.gen != src.gen:
            return {}            # read from an older state (an edit was applied meanwhile): dropped
        return {k: (LoadFailed(v) if isinstance(v, BaseException) else v) for k, v in out.items()}

    def _peek_info(self, kp: tuple) -> NodeInfo | None:
        """Loaded info of ``kp`` without touching the loader's queue (loader first, then the
        infos kept from tasks and from before the last edit)."""
        value = self._loader.peek(kp)
        if isinstance(value, NodeInfo):
            return value
        return self._known.get(kp)

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
        if self._pending_command is not None:
            self._run_pending_command()
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
        self._sync_source()
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
            value = self._known.get(kp)      # kept from a task or from before an edit (being fetched)
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
            if info.smart is not None:   # a time / date / link / colour / image (treemodel.smart_value)
                decor = smart_cell(info.smart, info.text, quoted=info.kind == "string")
                return Cell(text=info.text, role=decor.pop("role", role), spans=spans, **decor)
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
            if is_edit_offset(info.start):     # an edited / new node: no position in the file
                tip = t("gui.tree.size.tip.edited", bytes=widgets.format_count(info.size))
            else:
                tip = t("gui.tree.size.tip", bytes=widgets.format_count(info.size),
                        start=widgets.format_count(info.start), end=widgets.format_count(info.end))
            return Cell(text=widgets.format_size(info.size), align="right", role="muted", tooltip=tip)
        if cid == COL_OFFSET:
            if is_edit_offset(info.start):
                return Cell(text=t("gui.tree.edited"), align="right", role="muted", italic=True,
                            tooltip=t("gui.tree.edited.tip"))
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
        keeps (or moves to its nearest visible ancestor), the selected nodes stay selected
        and visible rows are requested."""
        if self._depth == 0:
            self._snapshot_selection()      # the rows still match the grid's selection
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
                    self.grid.set_current(row, 0, select=True, scroll="none")   # also the Shift anchor
            self._reapply_selection()
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

    # ------------------------------------------------------------------ multi-selection (§12.9)
    def _on_grid_selection(self) -> None:
        if self._syncing or self._dead:
            return
        if not self._selecting_all:
            self._sel_all_asked = False
        self._sel_dirty = True
        self.selection_changed.emit()

    def select_all(self) -> None:
        """Ctrl+A: select every row; the selection stays "everything" while rows are expanded,
        collapsed or edited (until another selection is made)."""
        self._selecting_all = True
        try:
            self.grid.select_all()
        finally:
            self._selecting_all = False
        self._sel_all_asked = True
        self._sel_dirty = True

    def _snapshot_selection(self) -> None:
        """Record the grid's selection as key paths (while the rows still match it)."""
        if not self._sel_dirty:
            return
        self._sel_dirty = False
        ranges = self.grid.selected_row_ranges()
        total = sum(b - a + 1 for a, b in ranges)
        rows = self.flat.row_count()
        self._sel_all = self._sel_all_asked and bool(rows) and total >= rows
        if total > EDIT_SELECTION_MAX:
            self._sel_kps = None
            return
        kps: list[tuple] = []
        for a, b in ranges:
            kps.extend(ri.keypath for ri in self.flat.iter_rows(a, b + 1))
        self._sel_kps = kps

    def _reapply_selection(self) -> None:
        """Select the recorded nodes again after the rows changed (inside ``_syncing``):
        hidden nodes leave the selection, "everything" stays everything, a big selection
        that cannot be followed shrinks to the current row."""
        grid = self.grid
        if self._sel_all:
            grid.select_all()
            return
        rows: list[int] = []
        keep: list[tuple] = []
        if self._sel_kps:
            with self._peeking():
                for kp in self._sel_kps:
                    row = self.flat.row_of(kp)
                    if row is not None:
                        rows.append(row)
                        keep.append(kp)
        if not rows:
            cur = grid.current()[0]
            self._sel_kps = []
            if cur >= 0:
                with contextlib.suppress(IndexError):
                    self._sel_kps = [self._locate(cur).keypath]
            grid.set_selected_ranges([(cur, cur)] if cur >= 0 else [])
            return
        order = sorted(range(len(rows)), key=rows.__getitem__)
        self._sel_kps = [keep[i] for i in order]
        grid.set_selected_ranges(_merge_rows(sorted(rows)))

    def selected_keypaths(self) -> list[tuple] | None:
        """The selected nodes for the node commands, in row order: without the wrapper /
        root rows and the wrapper's record container, without descendants of selected
        nodes. ``None`` when more than :data:`EDIT_SELECTION_MAX` rows are selected (only
        record commands can take such a selection)."""
        self._snapshot_selection()
        if self._sel_kps is None:
            return None
        return self._normalize(self._sel_kps)

    def _normalize(self, kps: list[tuple]) -> list[tuple]:
        chosen: set[tuple] = set()
        for kp in sorted(set(kps), key=len):
            if self._fixed_row(kp) or any(kp[:k] in chosen for k in range(1, len(kp))):
                continue
            chosen.add(kp)
        return [kp for kp in dict.fromkeys(kps) if kp in chosen]

    def bulk_selection(self) -> Any:
        """The records holding the selected rows -- the bulk dialog's "selected records" scope
        (a :class:`~jsonreshape.gui.bulkdialog.SelectionScope` of the state shown; ``None`` when
        no record row or node of a record is selected). Selections beyond
        :data:`EDIT_SELECTION_MAX` rows are passed as row ranges of the view (resolved in the
        preview task)."""
        from jsonreshape.gui.bulkdialog import SelectionScope

        if not self._ready() or self._kind == "single":
            return None
        edit = self._edit()
        gen = getattr(edit, "applied_gen", None) if edit is not None else None
        if gen is None:
            gen = getattr(self.session, "data_gen", None)
        kps = self.selected_keypaths()
        if kps is not None:
            positions = sorted({int(kp[0]) for kp in kps
                                if isinstance(kp[0], int) and not isinstance(kp[0], bool)})
            return SelectionScope(positions=positions, gen=gen) if positions else None
        off = self._offset()
        rows: list[tuple[int, int]] = []
        extra: set[int] = set()
        for a, b in self.grid.selected_row_ranges():
            with contextlib.suppress(IndexError):
                ri = self._locate(a)
                top = ri.keypath[0]
                if ri.depth and isinstance(top, int) and not isinstance(top, bool):
                    extra.add(top)          # a range starting inside a record counts that record
            for _row, pos, n in self.flat.top_runs(a, b):
                if off and pos == 0:
                    pos, n = 1, n - 1
                if n > 0:
                    rows.append((pos - off, pos - off + n - 1))
        if not rows and not extra:
            return None
        s = self.session
        source = None if s.view_is_base else s.view_recordset()
        if source is not None:
            gen = getattr(source, "gen", gen)
        return SelectionScope(source=source, rows=rows, positions=sorted(extra) or None, gen=gen)

    def _fixed_row(self, kp: tuple) -> bool:
        """A row no node command takes as a target: the wrapper object, a single document's
        root, the wrapper's record container."""
        if len(kp) == 1 and kp[0] in (WRAPPER, ROOT):
            return True
        info = self._peek_info(kp)
        return info is not None and info.records

    def select_keypaths(self, kps: list[tuple]) -> None:
        """Select these nodes (the visible ones; tests and programmatic selections). The first
        one becomes the current node."""
        kps = [tuple(kp) for kp in kps]
        rows = []
        with self._peeking():
            for kp in kps:
                row = self.flat.row_of(kp)
                if row is not None:
                    rows.append(row)
        if not rows:
            return
        self.select_row(rows[0], scroll="none")
        self._syncing = True
        try:
            self.grid.set_selected_ranges(_merge_rows(sorted(rows)))
        finally:
            self._syncing = False
        self._sel_dirty = True
        self.selection_changed.emit()

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
    def reveal(self, ref: NodeRef, *, select: bool = False, center: bool = True, quiet: bool = False) -> None:
        """Show ``ref``: resolve it (worker), make sure its record is in the view, expand
        its ancestors (children tasks for large ones), then make it the current row
        (scrolled to the center). ``select``: also select it in the session (a user
        action of this view, e.g. a breadcrumb click). ``quiet``: no "not in this view"
        message (the node an edit selected may be outside a filtered view)."""
        if self._dead:
            return
        if not self._active or not self._ensure_doc():
            self._follow_pending = ref
            return
        self._reveal_gen += 1
        gen = self._reveal_gen
        self._reveal_ref = ref
        self._reveal_quiet = quiet
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
            if self._reveal_quiet:
                return
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
                if not self._reveal_quiet:
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
            if not self._reveal_quiet:
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

    # ================================================================== editing (§12.9)
    def _edit(self) -> Any:
        return getattr(self.session, "edit", None)

    def _edit_block(self) -> str | None:
        """Why no edit command can run now (``None``: they can; a busy controller refuses
        the command itself with a status message)."""
        if not self._ready():
            return t("gui.tree.edit.unavailable")
        edit = self._edit()
        if edit is None:
            return t("gui.tree.edit.unavailable")
        if not getattr(edit, "available", False):
            fn = getattr(actions, "edit_reason_text", None)
            if callable(fn):
                with contextlib.suppress(Exception):
                    return str(fn(edit))
            return t("gui.tree.edit.unavailable")
        return None

    def _current(self) -> tuple[tuple, NodeInfo] | None:
        """The current node with its loaded data (``None``: no current row or not loaded)."""
        kp = self._visible_current()
        if kp is None:
            return None
        info = self._peek_info(kp)
        return None if info is None else (kp, info)

    def _target_current(self, retry: Callable[[], Any] | None = None) -> tuple[tuple, NodeInfo] | None:
        """:meth:`_current` for a command: ``None`` after telling the user why. When the
        current row's data is still being read (e.g. right after an edit) and ``retry`` is
        given, ``retry()`` runs once it arrives -- if that node is still current then."""
        why = self._edit_block()
        if why is not None:
            self._notify(why)
            return None
        self._sync_source()
        kp = self._visible_current()
        if kp is None:
            self._notify(t("gui.tree.edit.no_node"))
            return None
        info = self._peek_info(kp)
        if info is None:
            if retry is not None:
                self._pending_command = (kp, retry)
                self._loader.get(kp)
            else:
                self._notify(t("gui.tree.edit.no_node"))
            return None
        self._pending_command = None
        return kp, info

    def _run_pending_command(self) -> None:
        """A command waiting for the current row's data (:meth:`_target_current`)."""
        pending = self._pending_command
        if pending is None:
            return
        kp, retry = pending
        if kp != self._cur_kp:
            self._pending_command = None          # the user moved on
            return
        if self._peek_info(kp) is None:
            if isinstance(self._loader.peek(kp), LoadFailed):
                self._pending_command = None
            return
        self._pending_command = None
        retry()

    def _locked(self, kp: tuple, info: NodeInfo) -> bool:
        """The wrapper object as a whole or its record container: their value cannot be
        replaced (the records live inside)."""
        return info.records or kp == (WRAPPER,)

    def _title(self, info: NodeInfo, key: str, ref: NodeRef | None = None) -> str:
        """A dialog title in the GUI's one format ``#12.name — …`` (the node's location as
        :meth:`DocSession.location_text` writes it; the path label without a ref)."""
        if ref is not None:
            return t(key, path=copyfmt.escape_surrogates(self.session.location_text(ref)))
        return t(key, path=_path_label(info.path))

    # -- edit value / as JSON -------------------------------------------------------------
    def edit_value(self) -> None:
        """Enter: edit the current node -- a short string or a number in a line editor over
        its value cell, a long / multi-line string in :class:`editors.StringDialog`, a bool is
        toggled, null and containers open :class:`editors.JsonEditDialog`. The value is read
        in a silent task first; the editor opens when it arrives and the node is still
        current."""
        self._start_edit("value")

    def edit_as_json(self) -> None:
        """Shift+Enter: edit the current node as JSON text (:class:`editors.JsonEditDialog`)."""
        self._start_edit("json")

    def _start_edit(self, mode: str) -> None:
        cur = self._target_current(lambda: self._start_edit(mode))
        if cur is None:
            return
        kp, info = cur
        if self._locked(kp, info):
            self._notify(t("gui.tree.edit.records_locked"))
            return
        edit = self._edit()
        ref = self._ref_for(kp, info)
        self._close_editor()
        if mode == "json" or info.kind in ("object", "array", "null"):
            self._json_dialog(ref, info)
            return
        if info.kind == "bool":
            edit.set_value(ref, "false" if info.text.strip() == "true" else "true")
            return
        self._load_gen += 1
        gen, data_gen = self._load_gen, self._data_gen
        src = self._src
        assert src is not None

        def work(ctx: Any) -> Any:
            return src.edit_payload(kp, ctx.cancel, token_max=TOKEN_MAX)

        def done(payload: Any) -> None:
            if self._dead or gen != self._load_gen or data_gen != self._data_gen or self._cur_kp != kp:
                return                       # another node / an edit came in between
            self._open_payload(kp, info, ref, payload)

        def failed(exc: BaseException) -> None:
            if self._dead or gen != self._load_gen:
                return
            from jsonreshape.errors import TooLargeError

            if isinstance(exc, TooLargeError):
                self.session.app.report_error(
                    JsonReshapeError("gui.tree.edit.too_large", size=exc.params.get("size", "?")), self)
                return
            widgets.report_task_error(self.session, exc, self, purpose="view")

        self.session.app.tasks.run(work, title=t("gui.tree.task.load_value"), kind="value", owner=self,
                                   silent=True, on_done=done, on_error=failed)

    def _open_payload(self, kp: tuple, info: NodeInfo, ref: NodeRef, payload: Any) -> None:
        edit = self._edit()
        if edit is None:
            return
        if payload.kind in ("object", "array", "null"):
            self._json_dialog(ref, info)
            return
        if payload.kind == "bool":
            edit.set_value(ref, "false" if (payload.token or "").strip() == "true" else "true")
            return
        if payload.kind == "string":
            if payload.value is None:        # longer than the line loader reads: the dialog's own loader
                self._string_dialog(ref, info, None)
                return
            value = payload.value
            if len(value) <= INLINE_MAX_CHARS and not _LINE_BREAK_RE.search(value) \
                    and self._inline(kp, COL_VALUE, "string", value, ref, "set_value"):
                return
            self._string_dialog(ref, info, value)
            return
        token = payload.token or ""
        if not self._inline(kp, COL_VALUE, "number", token, ref, "set_value"):
            self._json_dialog(ref, info)

    # -- open editors and their nodes (review3 L2) -------------------------------------------
    def _pending_edit(self, ref: NodeRef | None) -> _Pending:
        p = _Pending(ref)
        self._pendings.append(p)
        return p

    def _release(self, p: _Pending) -> None:
        with contextlib.suppress(ValueError):
            self._pendings.remove(p)

    def _attach(self, p: _Pending, dialog: Any) -> Any:
        """``dialog`` acts on ``p``'s node: ``p`` is followed until the dialog closes."""
        p.editor = dialog
        finished = getattr(dialog, "finished", None)
        if finished is not None:
            finished.connect(lambda _result=0, p=p: self._release(p))
        return dialog

    def _follow_pendings(self, cs: Any | None) -> None:
        """An applied ChangeSet (``None``: the document was reset / reopened): the nodes of the open
        editors follow it (deleted -> ``None``); their key checks are asked again on the new data."""
        for p in list(self._pendings):
            if p.editor is not None and not shiboken6.isValid(p.editor):
                self._release(p)
                continue
            if p.ref is not None:
                p.ref = None if cs is None else self._remap_noderef(p.ref, cs)
            forget = getattr(p.editor, "forget_key_checks", None)
            if callable(forget):
                try:
                    forget()
                except RuntimeError:          # the dialog is being destroyed
                    self._release(p)

    def _command_for(self, ref: NodeRef, name: str) -> tuple[_Pending, Callable[[str], Any]]:
        """``(pending, fn)``: ``fn(text)`` runs ``edit.<name>(node, text)`` on the node ``ref`` names when
        the editor commits -- ``pending.ref``, remapped by every edit applied while the editor was open
        (review3 L2); a node deleted meanwhile (or a document reset / reopened since) is reported."""
        edit = self._edit()
        p = self._pending_edit(ref)

        def run(text: str) -> None:
            self._release(p)
            if self._dead:
                return
            if p.ref is None or edit is None or edit is not self._edit():
                self._notify(t("gui.tree.edit.gone"))
                return
            getattr(edit, name)(p.ref, text)

        return p, run

    # -- key checks (review3 L1) ---------------------------------------------------------------
    def _key_target(self, ref: NodeRef | None, *, own: bool = False) -> Any:
        """Where the keys a key of ``ref`` must differ from live: the key path of the object holding
        ``ref`` (``own``: of ``ref`` itself), :data:`_RECORD_KEYS` for a record of a member-record
        document, ``None`` when there are no keys to compare (or the node is unknown)."""
        if ref is None:
            return None
        kp = self._kp_for_ref(ref)
        if kp is None:
            return None
        if own:
            return kp
        if len(kp) == 1:
            top = kp[0]
            ok = isinstance(top, int) and not isinstance(top, bool) and self._member_records()
            return _RECORD_KEYS if ok else None
        return kp[:-1]

    def _key_check(self, target: Callable[[], Any]) -> Callable[[str, Callable[[bool], None]], None]:
        """The editors' ``key_check``: is ``key`` a key of the object ``target()`` names (a key path, or
        :data:`_RECORD_KEYS`) in the state shown now? Looked up in a silent task
        (``Node.find_child_index``; record keys through the document's key index when it has one)."""

        def check(key: str, reply: Callable[[bool], None]) -> None:
            where, src = target(), self._src
            if where is None or src is None or self._dead:
                reply(False)
                return

            def work(ctx: Any) -> bool:
                node = src.doc.records_node() if where is _RECORD_KEYS else src.node(where, ctx.cancel)
                if node is None or node.kind != "object":
                    return False
                return node.find_child_index(key, cancel=ctx.cancel) is not None

            self.session.app.tasks.run(work, title=t("gui.tree.task.key_check"), kind="value", owner=self,
                                       silent=True, on_done=lambda taken: reply(bool(taken)),
                                       on_error=lambda _exc: reply(False))

        return check

    # -- editors (gui/editors.py) ----------------------------------------------------------
    def _column_index(self, cid: str) -> int | None:
        for i, col in enumerate(self.grid.columns()):
            if col.id == cid:
                return i if col.visible else None
        return None

    def _inline(self, kp: tuple, cid: str, kind: str, text: str, ref: NodeRef, name: str, *,
                taken: Any = None, key_check: Any = None) -> _Pending | None:
        """Open the line editor over the cell ``cid`` of ``kp``'s row; committing runs
        ``edit.<name>(ref, value)``. Returns the editor's pending node; ``None`` when it cannot be
        shown there (column hidden, row not visible)."""
        from jsonreshape.gui import editors

        col = self._column_index(cid)
        row = self.flat.row_of(kp)
        if col is None or row is None:
            return None
        self.grid.scroll_to(row, "ensure")
        if cid != COL_KEY:
            self.grid.scroll_to_column(col)
        self._close_editor()
        p, commit = self._command_for(ref, name)

        def on_commit(value: Any) -> None:
            if isinstance(value, (bytes, bytearray)):
                value = bytes(value).decode("utf-8", "surrogatepass")
            self._editor_done()
            commit(str(value))

        def on_cancel() -> None:
            self._release(p)
            self._editor_done()

        self._editor_kp = kp
        self._editor = editors.InlineEditor(self.grid, row, col, kind, text, on_commit, on_cancel, taken=taken,
                                            key_check=key_check)
        p.editor = self._editor
        return p

    def _editor_done(self) -> None:
        self._editor = None
        self._editor_kp = None

    def _close_editor(self) -> None:
        """Cancel an open inline editor (the rows change under it)."""
        ed, self._editor = self._editor, None
        self._editor_kp = None
        if ed is not None:
            with contextlib.suppress(RuntimeError, AttributeError):
                ed.cancel()

    def inline_editor(self) -> Any:
        """The open inline editor (``None``: none; tests)."""
        return self._editor

    def _string_dialog(self, ref: NodeRef, info: NodeInfo, value: str | None) -> Any:
        """:class:`editors.StringDialog` for the string of ``ref`` (``value`` ``None``: the
        dialog reads it itself)."""
        from jsonreshape.gui import editors

        title = self._title(info, "gui.tree.edit.title.value", ref)
        p, accept = self._command_for(ref, "set_value")
        if value is None:
            return self._attach(p, editors.StringDialog.for_node(self.session, ref, parent=self, title=title,
                                                                 on_accept=accept))
        dlg = editors.StringDialog(self, value, title=title, on_accept=accept)
        self._attach(p, dlg)
        _open_dialog(dlg)
        return dlg

    def _json_dialog(self, ref: NodeRef, info: NodeInfo) -> Any:
        """:class:`editors.JsonEditDialog` with the effective text of ``ref`` (read by the
        dialog's loader)."""
        from jsonreshape.gui import editors

        p, accept = self._command_for(ref, "set_value")
        return self._attach(p, editors.JsonEditDialog.for_node(
            self.session, ref, parent=self, title=self._title(info, "gui.tree.edit.title.json", ref), indent=2,
            on_accept=accept))

    # -- rename ------------------------------------------------------------------------------
    def rename_key(self) -> None:
        """F2: rename the current object member (or keyed record) in a line editor over its
        key cell (:class:`editors.KeyDialog` for long / multi-line keys)."""
        cur = self._target_current(self.rename_key)
        if cur is None:
            return
        kp, info = cur
        why = self._rename_block(kp, info)
        if why is not None:
            self._notify(why)
            return
        ref = self._ref_for(kp, info)
        old = str(info.key)
        self._close_editor()
        holder: list[_Pending] = []

        def target() -> Any:                  # the object (or the records) the key must be unique in
            return self._key_target(holder[0].ref if holder else ref)

        taken = _LoadedKeys(self, target)
        check = self._key_check(target)
        if len(old) <= INLINE_MAX_CHARS and not _LINE_BREAK_RE.search(old):
            inline = self._inline(kp, COL_KEY, "key", old, ref, "rename_key", taken=taken, key_check=check)
            if inline is not None:
                holder.append(inline)
                return
        from jsonreshape.gui import editors

        p, commit = self._command_for(ref, "rename_key")
        holder.append(p)
        dlg = editors.KeyDialog(self, old, title=self._title(info, "gui.tree.edit.title.key", ref), on_accept=commit,
                                taken=taken, key_check=check)
        self._attach(p, dlg)
        _open_dialog(dlg)

    def _rename_block(self, kp: tuple, info: NodeInfo) -> str | None:
        if info.records:
            return t("gui.tree.edit.records_locked")
        if not isinstance(info.key, str) or (len(kp) == 1 and kp[0] in (WRAPPER, ROOT)):
            return t("gui.tree.edit.no_key")
        return None

    # -- insert ------------------------------------------------------------------------------
    def insert_menu(self) -> QMenu | None:
        """Insert: a menu "child / before / after" at the current row (record rows: a new
        record before / after). Returns the (shown) menu."""
        cur = self._target_current(self.insert_menu)
        if cur is None:
            return None
        kp, info = cur
        menu = QMenu(self)
        menu.setObjectName("tree_insert_menu")
        menu.setToolTipsVisible(True)
        top = len(kp) == 1 and isinstance(kp[0], int) and not isinstance(kp[0], bool)
        for where in ("child", "before", "after"):
            key = f"gui.tree.edit.insert.{'record_' + where if top and where != 'child' else where}"
            act = menu.addAction(t(key))
            act.setObjectName(f"tree_insert_{where}")
            act.triggered.connect(lambda _checked=False, w=where: self.insert_node(w))
            why = self._insert_block(kp, info, where)
            if why is not None:
                act.setEnabled(False)
                act.setToolTip(why)
        menu.aboutToHide.connect(menu.deleteLater)
        rect = self.grid.row_rect(self.flat.row_of(kp) or 0)
        menu.popup(self.grid.viewport().mapToGlobal(rect.bottomLeft() + QPoint(self.grid.gutter_width() + 24, 0)))
        return menu

    def _insert_block(self, kp: tuple, info: NodeInfo, where: str) -> str | None:
        if where == "child":
            if not info.is_container or info.records:
                return t("gui.tree.edit.records_locked") if info.records else t("gui.tree.edit.need_container")
            return None
        if len(kp) == 1:
            if kp[0] in (WRAPPER, ROOT):
                return t("gui.tree.edit.root_no_siblings")
            return None
        if self._peek_info(kp[:-1]) is None:
            return t("gui.tree.edit.no_node")
        return None

    def insert_node(self, where: str = "child") -> Any:
        """Insert a new node ``"child"`` (at the end of the current container), ``"before"``
        or ``"after"`` the current node (top rows: a new record), asking position, key, type
        and value with :class:`editors.InsertDialog` (``where`` preselected; the other
        possible positions stay selectable). Returns the dialog."""
        cur = self._target_current(lambda: self.insert_node(where))
        if cur is None:
            return None
        kp, info = cur
        options = self._insert_options(kp, info)
        if where not in options:
            self._notify(self._insert_block(kp, info, where) or t("gui.tree.edit.unavailable"))
            return None
        from jsonreshape.gui import editors

        edit = self._edit()
        # the dialog follows the current node through edits applied while it is open (review3 L2):
        # parent and index are computed from that node when OK is pressed
        p = self._pending_edit(self._ref_for(kp, info))

        def accept(result: Any) -> None:
            self._release(p)
            if self._dead:
                return
            if p.ref is None or edit is None or edit is not self._edit():
                self._notify(t("gui.tree.edit.gone"))
                return
            parent, index = _insert_place(p.ref, result.position)
            edit.insert(parent, index, result.value, key=result.key if options[result.position][2] else None)

        def target(position: str) -> Callable[[], Any]:
            return lambda: self._key_target(p.ref, own=position == "child")

        dlg = editors.InsertDialog(self, title=self._title(info, "gui.tree.edit.title.insert", p.ref),
                                   positions=tuple(options), position=where,
                                   key_needed={pos: o[2] for pos, o in options.items()}, on_accept=accept,
                                   taken={pos: _LoadedKeys(self, target(pos)) for pos in options},
                                   key_check={pos: self._key_check(target(pos)) for pos in options})
        self._attach(p, dlg)
        _open_dialog(dlg)
        return dlg

    def _insert_options(self, kp: tuple, info: NodeInfo) -> dict[str, tuple[NodeRef | None, int | None, bool]]:
        """``position -> (parent ref | None = the record level, index | None = end, key needed)``
        for the insert positions possible at ``kp``."""
        out: dict[str, tuple[NodeRef | None, int | None, bool]] = {}
        for where in ("before", "after", "child"):
            if self._insert_block(kp, info, where) is not None:
                continue
            if where == "child":
                out[where] = (self._ref_for(kp, info), None, info.kind == "object")
            elif len(kp) == 1:
                out[where] = (None, int(kp[0]) + (1 if where == "after" else 0), self._member_records())
            else:
                pinfo = self._peek_info(kp[:-1])
                assert pinfo is not None
                out[where] = (self._ref_for(kp[:-1], pinfo), int(kp[-1]) + (1 if where == "after" else 0),
                              pinfo.kind == "object")
        return out

    def _member_records(self) -> bool:
        doc = self._doc
        return bool(doc is not None and getattr(doc, "member_records", False))

    # -- duplicate / delete --------------------------------------------------------------------
    def delete_selection(self) -> None:
        """Del: delete the selected nodes / records (more than :data:`EDIT_SELECTION_MAX`
        record rows: ``delete_records`` as a task)."""
        self._selection_command("delete")

    def duplicate_selection(self) -> None:
        """Ctrl+D: a copy right after each selected node / record."""
        self._selection_command("duplicate")

    def _selection_command(self, op: str) -> None:
        why = self._edit_block()
        if why is not None:
            self._notify(why)
            return
        self._sync_source()
        edit = self._edit()
        kps = self.selected_keypaths()
        if kps is None:
            runs = self._record_runs()
            if runs is None:
                self._notify(t("gui.tree.edit.too_many", limit=widgets.format_count(EDIT_SELECTION_MAX)))
                return
            if self._kind == "single":
                self._notify(t("gui.tree.edit.root_no_siblings"))
                return
            self._with_positions(runs, getattr(edit, f"{op}_records"))
            return
        if not kps:
            self._notify(self._fixed_reason() or t("gui.tree.edit.nothing_selected"))
            return
        self._with_refs(kps, getattr(edit, op))

    def _fixed_why(self, kp: tuple) -> str | None:
        """Why the row ``kp`` is no target of the node commands (delete, duplicate, move), or
        ``None``: the wrapper object, a single document's root, the wrapper's record container."""
        if len(kp) == 1 and kp[0] == WRAPPER:
            return t("gui.tree.edit.fixed.wrapper")
        if len(kp) == 1 and kp[0] == ROOT:
            return t("gui.tree.edit.fixed.root")
        info = self._peek_info(kp)
        if info is not None and info.records:
            return t("gui.tree.edit.fixed.records")
        return None

    def _fixed_reason(self) -> str | None:
        """Why the selection holds no node for a node command although rows are selected: the
        reason of its first protected row (review3 L7); ``None`` when nothing is selected."""
        self._snapshot_selection()
        kps = list(self._sel_kps or ())
        if not kps and self._cur_kp is not None:
            kps = [self._cur_kp]
        for kp in kps:
            why = self._fixed_why(kp)
            if why is not None:
                return why
        return None

    def _record_runs(self) -> list[tuple[int, int]] | None:
        """``[(view position, n)]`` of a selection that holds top rows only (the wrapper row
        left out); ``None`` when nested rows are selected too."""
        ranges = self.grid.selected_row_ranges()
        total = sum(b - a + 1 for a, b in ranges)
        runs = [(pos, n) for a, b in ranges for _row, pos, n in self.flat.top_runs(a, b)]
        if sum(n for _p, n in runs) != total:
            return None
        off = self._offset()
        out: list[tuple[int, int]] = []
        for pos, n in runs:
            if off and pos == 0:
                pos, n = 1, n - 1
            if n > 0:
                out.append((pos - off, n))
        return out

    def _with_positions(self, runs: list[tuple[int, int]], fn: Callable[[Any], Any]) -> None:
        """``fn(positions)`` with the record positions of view-position runs: the base view
        passes them lazily (any size), other views read them from the view's record set in a
        task first."""
        s = self.session
        if s.view_is_base:
            fn(chain.from_iterable(range(p, p + n) for p, n in runs))
            return
        rs = s.view_recordset()
        if rs is None:
            return
        gen = self._data_gen

        def work(ctx: Any) -> Any:
            from array import array

            out = array("Q")
            for p, n in runs:
                for a in range(p, p + n, 1 << 16):
                    check(ctx.cancel)
                    out.extend(rs[a:min(p + n, a + (1 << 16))])
            return out

        def done(positions: Any) -> None:
            if not self._dead and gen == self._data_gen:
                fn(positions)

        self.session.app.tasks.run(work, title=t("gui.tree.edit.task.positions"), kind="value", owner=self,
                                   silent=True, on_done=done,
                                   on_error=lambda exc: widgets.report_task_error(self.session, exc, self,
                                                                                   purpose="view"))

    def _with_refs(self, kps: list[tuple], fn: Callable[[list[NodeRef]], Any]) -> None:
        """``fn(refs)`` with the NodeRefs of ``kps`` (rows whose data is not loaded are read in
        a silent task first; the wrapper's record container is left out)."""
        refs: list[NodeRef | None] = []
        missing: list[tuple] = []
        for kp in kps:
            top = kp[0]
            if len(kp) == 1 and isinstance(top, int) and not isinstance(top, bool):
                refs.append(NodeRef(top, (), ()))
                continue
            info = self._peek_info(kp)
            if info is None:
                missing.append(kp)
                refs.append(None)
            elif not info.records:
                refs.append(self._ref_for(kp, info))
            else:
                refs.append(None)
        if not missing:
            done = [r for r in refs if r is not None]
            if done:
                fn(done)
            return
        src, gen = self._src, self._data_gen
        assert src is not None

        def work(ctx: Any) -> dict:
            return src.fetch(missing, ctx.cancel)

        def done_fetch(found: dict) -> None:
            if self._dead or gen != self._data_gen:
                return
            out: list[NodeRef] = []
            for kp, ref in zip(kps, refs):
                if ref is None:
                    got = found.get(kp)
                    if not isinstance(got, NodeInfo) or got.records:
                        continue
                    self._remember(got)
                    ref = self._ref_for(kp, got)
                out.append(ref)
            if out:
                fn(out)

        self.session.app.tasks.run(work, title=t("gui.tree.edit.task.refs"), kind="value", owner=self, silent=True,
                                   on_done=done_fetch,
                                   on_error=lambda exc: widgets.report_task_error(self.session, exc, self,
                                                                                   purpose="view"))

    # -- move --------------------------------------------------------------------------------
    def move_selection(self, delta: int) -> None:
        """Alt+Up / Alt+Down: move the selected siblings one place (record rows only in the
        base view; not while their parent shows a display order)."""
        why = self._edit_block()
        if why is not None:
            self._notify(why)
            return
        self._sync_source()
        kps, why = self._move_targets()
        if why is not None:
            self._notify(why)
            return
        edit = self._edit()
        self._with_refs(kps, lambda refs: edit.move_by(refs, -1 if delta < 0 else 1))

    def _move_targets(self) -> tuple[list[tuple], str | None]:
        kps = self.selected_keypaths()
        if kps is None:
            return [], t("gui.tree.edit.too_many", limit=widgets.format_count(EDIT_SELECTION_MAX))
        if not kps:
            return [], self._fixed_reason() or t("gui.tree.edit.nothing_selected")
        parents = {kp[:-1] for kp in kps}
        if len(parents) != 1:
            return [], t("gui.tree.edit.not_siblings")
        parent = next(iter(parents))
        if not parent:
            if self._kind == "single":
                return [], t("gui.tree.edit.root_no_siblings")
            if not self.session.view_is_base:
                return [], t("gui.tree.edit.view_records")
        elif self.flat.has_order(parent):
            return [], t("gui.tree.edit.sorted_parent")
        return kps, None

    # -- change type / paste -----------------------------------------------------------------
    def change_type(self, kind: str) -> None:
        """Convert the selected nodes (or the current one) to ``kind`` (§12.4 table)."""
        why = self._edit_block()
        if why is not None:
            self._notify(why)
            return
        self._sync_source()
        kps = self.selected_keypaths()
        if kps is None:
            self._notify(t("gui.tree.edit.too_many", limit=widgets.format_count(EDIT_SELECTION_MAX)))
            return
        if not kps:
            cur = self._current()
            if cur is None or self._fixed_row(cur[0]):
                self._notify(t("gui.tree.edit.nothing_selected"))
                return
            kps = [cur[0]]
        edit = self._edit()
        self._with_refs(kps, lambda refs: edit.change_type(refs, kind))

    def change_type_kinds(self) -> tuple[str, ...] | None:
        """The kinds of the nodes :meth:`change_type` would convert (the selection, else the
        current node) for the menus' rules; ``None`` when one of them is not loaded yet."""
        kps = self.selected_keypaths()
        if kps is None:
            return None
        if not kps:
            cur = self._current()
            if cur is None or self._fixed_row(cur[0]):
                return None
            kps = [cur[0]]
        kinds: dict[str, None] = {}
        for kp in kps:
            info = self._peek_info(kp)
            if info is None:
                return None
            kinds[info.kind] = None
        return tuple(kinds)

    def paste_json(self) -> None:
        """Ctrl+V: insert the clipboard's JSON text as a new child of the current container
        (objects: a free key ``pasted``, ``pasted_2``, ...) or right after the current scalar
        (record rows: as a new record)."""
        cur = self._target_current(self.paste_json)
        if cur is None:
            return
        text = QGuiApplication.clipboard().text()
        if not text.strip():
            self._notify(t("gui.tree.edit.clipboard_empty"))
            return
        kp, info = cur
        edit = self._edit()
        if info.records:
            edit.paste(None, text)                                  # a new record at the end
        elif info.is_container:
            edit.paste(self._ref_for(kp, info), text)
        elif len(kp) == 1:
            if kp[0] in (WRAPPER, ROOT):
                self._notify(t("gui.tree.edit.root_no_siblings"))
                return
            edit.paste(None, text, index=int(kp[0]) + 1)
        else:
            pinfo = self._peek_info(kp[:-1])
            if pinfo is None:
                self._notify(t("gui.tree.edit.no_node"))
                return
            edit.paste(self._ref_for(kp[:-1], pinfo), text, index=int(kp[-1]) + 1)

    # -- menu / command states (actions.EDIT_COMMANDS) ---------------------------------------
    def edit_command_state(self, name: str) -> tuple[bool, str] | None:
        """``(enabled, reason)`` of an edit command for the current node / selection (the
        node menu and the main window's Edit menu ask)."""
        why = self._edit_block()
        if why is not None:
            return False, why
        if name in ("duplicate", "delete"):
            kps = self.selected_keypaths()
            if kps is None:
                if self._record_runs() is None:
                    return False, t("gui.tree.edit.too_many", limit=widgets.format_count(EDIT_SELECTION_MAX))
                return True, ""
            return (True, "") if kps else (False, self._fixed_reason() or t("gui.tree.edit.nothing_selected"))
        if name in ("move_up", "move_down"):
            _kps, why = self._move_targets()
            return (False, why) if why is not None else (True, "")
        cur = self._current()
        if cur is None:
            return False, t("gui.tree.edit.no_node")
        kp, info = cur
        if name in ("edit_value", "edit_json"):
            return (False, t("gui.tree.edit.records_locked")) if self._locked(kp, info) else (True, "")
        if name == "rename":
            why = self._rename_block(kp, info)
            return (False, why) if why is not None else (True, "")
        if name.startswith("insert_"):
            why = self._insert_block(kp, info, name[len("insert_"):])
            return (False, why) if why is not None else (True, "")
        if name == "change_type":
            kps = self.selected_keypaths()
            if kps is None:
                return False, t("gui.tree.edit.too_many", limit=widgets.format_count(EDIT_SELECTION_MAX))
            if not kps and self._fixed_row(kp):
                return False, t("gui.tree.edit.records_locked")
            return True, ""
        if name == "paste":
            if not info.is_container and len(kp) == 1 and kp[0] in (WRAPPER, ROOT):
                return False, t("gui.tree.edit.root_no_siblings")
            return True, ""
        return None

    # -- drag & drop (VirtualGrid.set_row_drag) ----------------------------------------------
    def _drag_can_start(self, ranges: list[tuple[int, int]]) -> bool:
        if self._edit_block() is not None or getattr(self._edit(), "busy", False):
            return False
        total = sum(b - a + 1 for a, b in ranges)
        if not total or total > EDIT_SELECTION_MAX:
            return False
        kps = [ri.keypath for a, b in ranges for ri in self.flat.iter_rows(a, b + 1)]
        nodes = self._normalize(kps)
        if not nodes:
            return False
        if any(len(kp) == 1 for kp in nodes) and not self.session.view_is_base:
            return False                     # record-level moves only in the base view
        self._drag_nodes = nodes
        return True

    def _drag_target(self, row: int, frac: float) -> tuple[int, str] | None:
        nodes = getattr(self, "_drag_nodes", None) or []
        try:
            kp = self._locate(row).keypath
        except IndexError:
            return None
        zone = drop_zone(frac)
        if zone == "after" and self.flat.is_expanded(kp) and row + 1 < self.flat.row_count() \
                and self.flat.parent_row(row + 1) == row:
            # below an open container's row = before its first child
            return self._drag_target(row + 1, 0.0)
        info = self._peek_info(kp)
        if info is None:
            return None
        if any(kp[:len(n)] == n for n in nodes):
            return None                      # into / next to its own subtree
        if zone == "inside":
            if not info.is_container or info.records:
                return None
            return row, "inside"
        if len(kp) == 1:
            if kp[0] in (WRAPPER, ROOT) or not self.session.view_is_base:
                return None
            return row, zone
        parent = kp[:-1]
        if self.flat.has_order(parent) or self._peek_info(parent) is None:
            return None
        return row, zone

    def _drag_drop(self, ranges: list[tuple[int, int]], row: int, zone: str) -> None:
        nodes = getattr(self, "_drag_nodes", None) or []
        self._drag_nodes = []
        if not nodes or self._edit_block() is not None:
            return
        try:
            kp = self._locate(row).keypath
        except IndexError:
            return
        edit = self._edit()
        if zone == "inside":
            info = self._peek_info(kp)
            if info is None:
                return
            parent: NodeRef | None = self._ref_for(kp, info)
            index: int | None = None
        elif len(kp) == 1:
            parent, index = None, int(kp[0]) + (1 if zone == "after" else 0)
        else:
            pinfo = self._peek_info(kp[:-1])
            if pinfo is None:
                return
            parent, index = self._ref_for(kp[:-1], pinfo), int(kp[-1]) + (1 if zone == "after" else 0)
        self._with_refs(nodes, lambda refs: edit.move(refs, parent, index))

    # -- applying a ChangeSet (session.data_changed) -----------------------------------------
    def _on_data_changed(self, cs: Any) -> None:
        if self._dead:
            return
        try:
            self._apply_change(cs)
        except Exception:  # noqa: BLE001 - never leave the view half-updated: start over
            log.exception("tree: applying an edit failed; the tree is rebuilt")
            self._reset_doc(self.session.doc)

    def _apply_change(self, cs: Any) -> None:
        """One applied ChangeSet (§12.9 "Tree"): stop what read the old state, carry the
        loaded rows of unchanged nodes over, re-key every key-path map and the FlatTree
        (record remap, then the NodeChanges in order), keep the top visible node in place,
        re-apply the selection and select ``cs.select``."""
        self._applied_gen = int(cs.gen)
        doc = self.session.doc
        if self._dead or doc is None or self._doc is None or doc is not self._doc:
            return                           # not showing this document yet: built fresh later
        if getattr(cs, "reset", False):
            self._reset_doc(doc)
            return
        # 1. everything that read the old state stops
        self._close_editor()
        self._load_gen += 1
        try:
            self.session.app.tasks.detach(self)
        except Exception:  # pragma: no cover - defensive
            log.debug("detach failed", exc_info=True)
        waiting: _Callbacks = [cb for cbs in self._busy.values() for cb in cbs]
        self._busy.clear()
        self._plan_task = None
        self._plan_gen += 1
        self._plan_auto = False
        self._close_plan_box()
        self._qf_task = None
        self._pos_task = None
        self._reveal_gen += 1
        self._reveal_ref = None
        self._view_gen += 1
        self._pos_cache.clear()
        self._pos_asked.clear()
        self._pos_wanted.clear()
        self._pos_retry.clear()
        self._seen_pos.clear()
        self._retry_timer.stop()
        # 2. how key paths move, which nodes changed
        fn = self._keypath_map(cs)
        changed = self._change_test(cs)
        nc_kps = [self._nc_keypath(nc) for nc in cs.nodes]
        anchor = self._scroll_anchor()
        self._snapshot_selection()
        carried = self._carry_over(fn, changed)
        # 3. a new source BEFORE the loader forgets (a fetch starting in between reads the new state)
        self._data_gen += 1
        old_src = self._src
        self._src = self._make_source(doc)
        if old_src is not None:
            for kp in old_src.recent_tops:
                new = fn(kp)
                if new is not None:
                    self._src.recent_tops.append(new)
        self._loader.invalidate()
        self._known = OrderedDict(carried)
        # 4. every key-path keyed state
        refresh = self._rekey(fn, changed, nc_kps)
        self._own_ref = self._remap_noderef(self._own_ref, cs)
        self._follow_pendings(cs)
        if self._follow_pending is not None:
            self._follow_pending = getattr(self.session, "current", None)
        # 5. the FlatTree
        with self._structure():
            if self._kind != "single":
                self.flat.remap_tops(cs.remap)
            for nc, ckp in zip(cs.nodes, nc_kps):
                try:
                    self.flat.remap_children(ckp, nc.remap, nc.count)
                except (ValueError, IndexError, TypeError):
                    log.debug("remap_children(%r) failed", ckp, exc_info=True)
            if self._active:
                self._sync_top()
        # 6. scroll position, selection, current node
        self._restore_anchor(anchor, fn)
        if cs.select is not None:
            self._select_after_change(cs.select)
        self._update_breadcrumb(self._own_ref if self._cur_kp is not None else None)
        # 7. follow-up work on the new state
        for kp in list(self._pending_expand):
            self._loader.get(kp)
        if self._pending_select is not None:
            self._loader.get(self._pending_select)
        if self._pending_command is not None:
            self._loader.get(self._pending_command[0])
        self._refresh_expanded(refresh)
        self.grid.refresh(request=True)
        if self.find_bar.isVisible() and self.find_bar.text():
            self._qf_timer.start()
        self.selection_changed.emit()
        _chain(waiting, failed=True)()

    def _nc_keypath(self, nc: Any) -> tuple:
        if self._kind == "single":
            top: Any = ROOT
        elif nc.record is None:
            top = WRAPPER
        else:
            top = int(nc.record)
        return (top,) + tuple(int(i) for i in nc.ipath)

    def _keypath_map(self, cs: Any) -> Callable[[tuple], tuple | None]:
        """Old key path -> new key path (``None``: the node is gone): the record remap, then
        every NodeChange in order (each container key path valid after the previous ones)."""
        remap = cs.remap if self._kind != "single" else None
        steps = [(self._nc_keypath(nc), nc.remap) for nc in cs.nodes]

        def fn(kp: tuple) -> tuple | None:
            top = kp[0]
            if remap is not None and isinstance(top, int) and not isinstance(top, bool):
                try:
                    top = remap.map(top)
                except (IndexError, ValueError):
                    top = None
                if top is None:
                    return None
                kp = (top,) + tuple(kp[1:])
            for ckp, rm in steps:
                n = len(ckp)
                if len(kp) > n and kp[:n] == ckp:
                    try:
                        c = rm.map(kp[n])
                    except (IndexError, ValueError):
                        c = None
                    if c is None:
                        return None
                    kp = kp[:n] + (c,) + kp[n + 1:]
            return kp

        return fn

    def _change_test(self, cs: Any) -> Callable[[tuple, NodeInfo | None], bool]:
        """``changed(new key path, info)``: the node's data may differ after ``cs`` (its record
        changed; the wrapper text changed; the wrapper's nodes holding the records -- their
        size and preview are built from the records -- whenever records changed)."""
        records = cs.records
        wrapper = bool(getattr(cs, "wrapper", False))
        any_records = records is None or bool(records) or cs.remap is not None
        base = tuple(self._src.record_base) if self._src is not None else ()

        def changed(kp: tuple, info: NodeInfo | None) -> bool:
            if records is None:
                return True
            top = kp[0]
            if top == ROOT:
                return 0 in records
            if top == WRAPPER:
                if wrapper:
                    return True
                if not any_records:
                    return False
                return info is None or tuple(info.path) == base[:len(info.path)]
            return top in records

        return changed

    def _carry_over(self, fn: Callable[[tuple], tuple | None],
                    changed: Callable[[tuple, NodeInfo | None], bool]) -> list[tuple[tuple, NodeInfo]]:
        """Loaded infos of unchanged nodes (the rows around the window, the current node and
        the selection, the infos kept from tasks) at their new key paths: the grid shows them
        while the loader reads the new state, so an edit does not blank the whole window."""
        out: dict[tuple, NodeInfo] = {}
        kps: list[tuple] = list(self._known)
        first, last = self.grid.visible_rows()
        if last >= first and self.flat.row_count():
            page = last - first + 1
            a = max(0, first - page)
            b = min(self.flat.row_count(), last + 1 + page, a + CARRY_ROWS)
            kps.extend(ri.keypath for ri in self.flat.iter_rows(a, b))
        if self._cur_kp is not None:
            kps.extend(self._cur_kp[:d] for d in range(1, len(self._cur_kp) + 1))
        if self._sel_kps:
            kps.extend(self._sel_kps[:CARRY_ROWS])
        for kp in kps:
            info = self._peek_info(kp)
            if info is None:
                continue
            new = fn(kp)
            if new is None or new in out or changed(new, info):
                continue
            out[new] = self._moved_info(info, new)
        return list(out.items())[-KNOWN_INFOS:]

    def _moved_info(self, info: NodeInfo, new: tuple) -> NodeInfo:
        """``info`` at the key path ``new`` (an unchanged node of a moved record: array-like
        documents number records by position, so the record's path segment / key follow)."""
        old = info.keypath
        if old == new:
            return info
        moved = dataclasses.replace(info, keypath=new)
        top_old, top_new = old[0], new[0]
        if top_old != top_new and isinstance(top_new, int) and not self._member_records():
            depth = (self._src.record_depth if self._src is not None else 1) - 1
            path = list(info.path)
            if len(path) > depth and path[depth] == top_old:
                path[depth] = top_new
                moved.path = tuple(path)
            if len(new) == 1 and info.key == top_old:
                moved.key = top_new
        return moved

    def _rekey(self, fn: Callable[[tuple], tuple | None], changed: Callable[[tuple, NodeInfo | None], bool],
               nc_kps: list[tuple]) -> list[tuple]:
        """Re-key every key-path keyed map with ``fn`` (``None``: forgotten). Display orders of
        containers whose children changed are dropped (the FlatTree shows file order), cached
        orders inside changed records too. Returns the expanded nodes (new key paths) whose
        kind / order must be read again."""

        def move(d: dict) -> dict:
            out = type(d)()
            for kp, value in d.items():
                new = fn(kp)
                if new is not None:
                    out[new] = value
            return out

        restructured = set(nc_kps)
        refresh: list[tuple] = list(dict.fromkeys(nc_kps))
        orders: OrderedDict[tuple, tuple[str, DisplayOrder]] = OrderedDict()
        entries = 0
        for kp, value in self._orders.items():
            new = fn(kp)
            if new is None or new in restructured or changed(new, None):
                if new is not None and new not in restructured:
                    refresh.append(new)
                continue
            orders[new] = value
            entries += len(value[1])
        self._orders, self._order_entries = orders, entries
        self._kinds = move(self._kinds)
        self._elem_sorts = move(self._elem_sorts)
        self._pending_expand = move(self._pending_expand)
        self._qf_matches = {new: v for kp, v in self._qf_matches.items()
                            if (new := fn(kp)) is not None and not changed(new, None)}
        self._qf_current = fn(self._qf_current) if self._qf_current is not None else None
        self._cur_kp = fn(self._cur_kp) if self._cur_kp is not None else None
        self._pending_select = fn(self._pending_select) if self._pending_select is not None else None
        if self._pending_command is not None:
            new = fn(self._pending_command[0])
            self._pending_command = None if new is None else (new, self._pending_command[1])
        if self._sel_kps is not None:
            self._sel_kps = list(dict.fromkeys(new for kp in self._sel_kps if (new := fn(kp)) is not None))
        if self._sort_keys or self._elem_sorts:
            for kp in self.flat.expanded_keypaths():
                new = fn(kp)
                if new is not None and changed(new, None):
                    refresh.append(new)
        return list(dict.fromkeys(refresh))[:2000]

    def _remap_noderef(self, ref: NodeRef | None, cs: Any) -> NodeRef | None:
        """A NodeRef after ``cs`` (record remap, then the record's NodeChanges: array-index
        segments of path and ipath rewritten); ``None``: the node is gone."""
        if ref is None:
            return None
        single = self._kind == "single"
        rec = ref.record
        if not single and rec is not None and cs.remap is not None:
            try:
                rec = cs.remap.map(int(rec))
            except (IndexError, ValueError):
                rec = None
            if rec is None:
                return None
        path = list(ref.path)
        ipath = None if ref.ipath is None else list(ref.ipath)
        if ipath is None or len(ipath) != len(path):
            return NodeRef(rec, tuple(path), None if ipath is None else tuple(ipath))
        for nc in cs.nodes:
            if not single and nc.record != rec:      # single documents: every NodeChange is of the root
                continue
            k = len(nc.ipath)
            if len(ipath) > k and tuple(ipath[:k]) == tuple(nc.ipath):
                try:
                    j = nc.remap.map(ipath[k])
                except (IndexError, ValueError):
                    j = None
                if j is None:
                    return None
                if isinstance(path[k], int) and not isinstance(path[k], bool):
                    path[k] = j
                ipath[k] = j
        return NodeRef(rec, tuple(path), tuple(ipath))

    def _scroll_anchor(self) -> list[tuple[tuple, int]]:
        """Key paths of the first rows on screen with their offset from the top row."""
        first, last = self.grid.visible_rows()
        if last < first or not self.flat.row_count():
            return []
        top = self.grid.top_row()
        return [(ri.keypath, ri.row - top) for ri in self.flat.iter_rows(first, min(last + 1, first + 64))]

    def _restore_anchor(self, anchor: list[tuple[tuple, int]], fn: Callable[[tuple], tuple | None]) -> None:
        """Scroll so that the first surviving anchor node is where it was on screen."""
        with self._peeking():
            for kp, offset in anchor:
                new = fn(kp)
                row = self.flat.row_of(new) if new is not None else None
                if row is not None:
                    self.grid.scroll_to(max(0, row - offset), "top")
                    return

    def _select_after_change(self, sel: Any) -> None:
        """Make ``cs.select`` (``(record, path, ipath)``, core convention) the current row:
        a node of the re-keyed selection keeps the selection (a block moved with Alt+arrows),
        anything else becomes the only selected node. The SESSION selects the node (after
        every view processed the ChangeSet: a ``current_changed`` sent from here would reach
        views that still hold refs of the old state -- review3 M2); this view already shows
        it then (``_own_ref``). Hidden views leave it to the session."""
        try:
            rec, path, ipath = sel
        except (TypeError, ValueError):
            return
        single = self._kind == "single"
        if single:
            top: Any = ROOT
        elif rec is None:
            top = WRAPPER
        else:
            top = int(rec)
        kp = (top,) + tuple(int(i) for i in ipath)
        ref = NodeRef(None if (single or rec is None) else int(rec), tuple(path), tuple(int(i) for i in ipath))
        if not self._active or not self.isVisible():
            return
        with self._peeking():
            row = self.flat.row_of(kp)
        if row is None:
            self.reveal(ref, select=True, center=False, quiet=True)
            return
        keep = bool(self._sel_kps) and kp in (self._sel_kps or [])
        self._cur_kp = kp
        self._syncing = True
        try:
            self.grid.set_current(row, 0, select=not keep, scroll="ensure")
        finally:
            self._syncing = False
        if not keep:
            self._sel_kps, self._sel_all, self._sel_dirty, self._sel_all_asked = [kp], False, False, False
        self._own_ref = ref

    def _refresh_expanded(self, kps: list[tuple]) -> None:
        """Expanded nodes whose children or values changed: read their kind again and
        compute the display orders their sorting needs (one silent task)."""
        kps = [kp for kp in kps if self.flat.is_expanded(kp)]
        if not kps or not self._ready():
            return
        src, gen = self._src, self._data_gen
        assert src is not None
        sort_keys = self._sort_keys
        elems = dict(self._elem_sorts)

        def work(ctx: Any) -> list[tuple]:
            out = []
            for kp in kps:
                check(ctx.cancel)
                try:
                    node = src.node(kp, ctx.cancel)
                except (IndexError, KeyError):
                    continue
                kind = node.kind
                order = mode = None
                count = None
                if kind in ("object", "array"):
                    count = int(node.child_count(cancel=ctx.cancel))
                    if kp in elems and kind == "array" and count:
                        field, desc = elems[kp]
                        order = DisplayOrder(src.element_order(node, count, SortKey(field, descending=desc),
                                                               ctx.cancel), count)
                        mode = "elements"
                    elif sort_keys and kind == "object" and count > 1:
                        order = DisplayOrder(src.key_order(node, count, ctx.cancel), count)
                        mode = "keys"
                out.append((kp, kind, count, order, mode))
            return out

        def done(result: list[tuple]) -> None:
            if self._dead or gen != self._data_gen:
                return
            with self._structure():
                for kp, kind, count, order, mode in result:
                    if not self.flat.is_expanded(kp):
                        continue
                    self._kinds[kp] = kind
                    if order is not None and self.flat.child_count(kp) == count:
                        self._store_order(kp, mode, order)
                        with contextlib.suppress(KeyError, ValueError):
                            self.flat.set_order(kp, order)

        self.session.app.tasks.run(work, title=t("gui.tree.task.refresh"), kind="children", owner=self, silent=True,
                                   on_done=done,
                                   on_error=lambda exc: log.debug("tree refresh after an edit failed: %r", exc))

    # ------------------------------------------------------------------ keyboard / mouse
    def _on_key(self, event: QKeyEvent) -> bool:
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all()
            return True
        key = event.key()
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        K = Qt.Key
        M = Qt.KeyboardModifier
        if self._edit_key(key, bool(mods & M.ControlModifier), bool(mods & M.AltModifier),
                          bool(mods & M.ShiftModifier), bool(mods & M.MetaModifier)):
            return True
        if mods & (M.ControlModifier | M.AltModifier | M.MetaModifier):
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

    def _edit_key(self, key: int, ctrl: bool, alt: bool, shift: bool, meta: bool) -> bool:
        """The editing keys (§12.9), handled before the plain-key handling: Return = edit,
        Shift+Return = edit as JSON, F2 = rename, Insert = insert menu, Delete, Ctrl+D =
        duplicate, Ctrl+V / Shift+Insert = paste, Alt+Up/Down = move. While editing is not
        available Return toggles a container as it did before editing existed."""
        K = Qt.Key
        if meta:
            return False
        if key in (K.Key_Return, K.Key_Enter) and not ctrl and not alt:
            if not shift:
                # a container that cannot be edited now -- or never as a whole (the wrapper object):
                # Enter toggles it, as before editing existed (review3 L7)
                kp = self._visible_current()
                info = self._peek_info(kp) if kp is not None else None
                if info is not None and info.expandable and (self._edit_block() is not None
                                                             or self._locked(kp, info)):
                    self.toggle_node(kp)
                    return True
            if shift:
                self.edit_as_json()
            else:
                self.edit_value()
            return True
        plain = not ctrl and not alt and not shift
        if plain and key == K.Key_F2:
            self.rename_key()
            return True
        if plain and key == K.Key_Insert:
            self.insert_menu()
            return True
        if plain and key == K.Key_Delete:
            self.delete_selection()
            return True
        if ctrl and not alt and not shift and key == K.Key_D:
            self.duplicate_selection()
            return True
        if (ctrl and not alt and not shift and key == K.Key_V) or (shift and not ctrl and not alt
                                                                   and key == K.Key_Insert):
            self.paste_json()
            return True
        if alt and not ctrl and not shift and key in (K.Key_Up, K.Key_Down):
            self.move_selection(-1 if key == K.Key_Up else 1)
            return True
        return False

    def _on_activated(self, row: Any, _col: int) -> None:
        """Double click (Enter is handled by the key handler): containers toggle, scalars
        open their editor."""
        if row is None or row < 0 or not self._ready():
            return
        try:
            kp = self.row_keypath(row)
        except IndexError:
            return
        info = self._peek_info(kp)
        if info is None or info.is_container:
            self.toggle_row(row)
            return
        if kp != self._cur_kp:
            self.select_row(row, scroll="none")
        self.edit_value()

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

    def _on_link(self, _row: Any, _col: int, url: str) -> None:
        """Ctrl+click on the link of a smart value: asked first, opened only after yes."""
        if not self._dead:
            widgets.open_url(self, url)

    # ------------------------------------------------------------------ compare the two selected
    def compare_refs(self) -> list[NodeRef] | None:
        """The NodeRefs of "Tanlangan ikkitasini taqqoslash": exactly two selected nodes
        (:meth:`selected_keypaths`, built like :meth:`ref_at`), else ``None`` (another count,
        or a selected row not loaded)."""
        if not self._ready():
            return None
        kps = self.selected_keypaths()
        if kps is None or len(kps) != 2:
            return None
        refs: list[NodeRef] = []
        for kp in kps:
            info = self._peek_info(kp)
            if info is None:
                return None
            refs.append(self._ref_for(kp, info))
        return refs

    def compare_selected(self) -> Any:
        """Compare the two selected nodes in a compare window
        (:func:`jsonreshape.gui.comparewindow.compare_selected`); a status message for another
        count. Returns the window (``None``: not started)."""
        refs = self.compare_refs()
        if refs is None:
            self._notify(t("gui.compare.menu.selected.need_two"))
            return None
        from jsonreshape.gui import comparewindow

        window = self.window()
        return comparewindow.compare_selected(window, self.session, refs, reveal=getattr(window, "show_node", None))

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
        compare = action(t("gui.compare.menu.selected"), "compare_selected", self.compare_selected,
                         enabled=self.compare_refs() is not None, why=t("gui.compare.menu.selected.need_two"))
        return [expand_all, collapse, levels, None, sort_menu, original, None, compare]

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

    # -- session restore / workspaces (stage 6, docs/ARCHITECTURE.md §15.0; gui/sessionstore.py) ------------
    def expanded_locations(self, limit: int = 2000) -> list[str]:
        """Location texts (:meth:`DocSession.location_text`) of the expanded nodes, parents first, at most
        ``limit``: what the session restore and workspaces keep. Only the loaded row data is used (no I/O): a
        node whose data is no longer loaded is left out -- except top-level records and the elements of
        expanded arrays, which are named without it. A single document's root (open on its own) and the
        wrapper's record container are not listed."""
        out: list[str] = []
        if not self._ready() or limit <= 0:
            return out
        named: dict[tuple, str] = {}
        session = self.session
        for kp in self.flat.expanded_keypaths():
            if len(out) >= limit:
                break
            top = kp[0]
            if kp == (ROOT,):
                named[kp] = "$"
                continue
            info = self._peek_info(kp)
            text: str | None = None
            if info is not None:
                if info.records:
                    continue
                text = session.location_text(self._ref_for(kp, info))
            elif len(kp) == 1:
                text = "$" if top == WRAPPER else f"#{top}" if isinstance(top, int) and not isinstance(top, bool) \
                    else None
            elif self._kinds.get(kp[:-1]) == "array" and kp[:-1] in named:
                parent = named[kp[:-1]]
                text = ("" if parent == "$" else parent) + f"[{kp[-1]}]"
            if text:
                named[kp] = text
                out.append(text)
        return out

    def restore_expanded(self, locations: Any, *, on_done: Callable[[int], Any] | None = None,
                         limit: int = 2000) -> bool:
        """Expand the nodes at ``locations`` (texts of :meth:`expanded_locations`): resolved, counted
        (children indexed where needed) and ordered like :meth:`expand_node` would, in ONE silent task, then
        expanded at once in the GUI thread -- no "expand N rows?" question (they were open). Nodes that no
        longer exist or are no containers are skipped (best effort; at most ``limit``). ``on_done(count)``
        once applied. False -- nothing started -- while the view has no ready document."""
        if not self._ready():
            return False
        texts = [x for x in locations if isinstance(x, str) and x][:max(0, int(limit))]
        src = self._src
        assert src is not None
        doc = self._doc
        session = self.session
        kind = self._kind
        sort_keys = self._sort_keys
        orders = dict(self._orders)
        elems = dict(self._elem_sorts)
        gen = self._data_gen

        def keypath(ref: NodeRef) -> tuple | None:
            path, ipath = tuple(ref.path), ref.ipath
            if ipath is None or len(ipath) != len(path):
                return None
            if ref.record is None or kind == "single":
                top: Any = ROOT if kind == "single" else WRAPPER if kind == "wrapped" else None
                if top is None:
                    return None
            else:
                top = int(ref.record)
            return (top,) + tuple(int(i) for i in ipath)

        def order_for(kp: tuple, node: Any, count: int, cancel: Any) -> Any:
            cached = orders.get(kp)
            if cached is not None and len(cached[1]) == count:
                return cached
            if kp in elems:
                field_, desc = elems[kp]
                order = src.element_order(node, count, SortKey(field_, descending=desc), cancel)
                return "elements", DisplayOrder(order, count)
            if sort_keys and node.kind == "object" and count > 1:
                return "keys", DisplayOrder(src.key_order(node, count, cancel), count)
            return None

        def work(ctx: Any) -> list[tuple]:
            from jsonreshape.errors import Cancelled
            from jsonreshape.paths import parse_location

            items: list[tuple] = []
            seen: set[tuple] = set()
            for text in texts:
                check(ctx.cancel)
                try:
                    kp = keypath(session.ref_for_location(parse_location(text), ctx.cancel))
                    if kp is None or kp in seen:
                        continue
                    seen.add(kp)
                    node = src.node(kp, ctx.cancel)
                    if node.kind not in ("object", "array") or src.is_records(node):
                        continue
                    count = int(node.child_count(cancel=ctx.cancel))
                    if count > 0:
                        items.append((kp, count, order_for(kp, node, count, ctx.cancel), node.kind))
                except Cancelled:
                    raise
                except Exception:  # noqa: BLE001 - gone / changed: skipped
                    log.debug("restore: %r is not expanded", text, exc_info=True)
            return items

        def done(items: list[tuple]) -> None:
            if self._dead or self._doc is not doc or gen != self._data_gen:
                return
            count = 0
            with self._structure():
                for kp, n, order, node_kind in items:
                    if self.flat.is_expanded(kp):
                        continue
                    arr = None
                    if order is not None:
                        mode, arr = order
                        self._store_order(kp, mode, arr)
                    if self._flat_expand(kp, n, arr, node_kind):
                        count += 1
            if on_done is not None:
                on_done(count)

        def failed(exc: BaseException) -> None:
            log.warning("restoring the expanded nodes of %s failed: %s", session.name, exc)

        self.session.app.tasks.run(work, title=t("gui.sessionstore.task.expand"), kind="children", owner=self,
                                   silent=True, on_done=done, on_error=failed)
        return True

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


_LINE_BREAK_RE = re.compile("[\n\r\x0b\x0c\x1c\x1d\x1e\x85  ]")


def _merge_rows(rows: list[int]) -> list[tuple[int, int]]:
    """Sorted rows -> inclusive ``(first, last)`` ranges of consecutive rows."""
    out: list[tuple[int, int]] = []
    for r in rows:
        if out and r <= out[-1][1] + 1:
            if r > out[-1][1]:
                out[-1] = (out[-1][0], r)
        else:
            out.append((r, r))
    return out


def _open_dialog(dlg: Any) -> None:
    """Show an editor dialog window-modal without a nested event loop (``open()``); it
    deletes itself when closed (its ``on_accept`` callback carries the result)."""
    with contextlib.suppress(AttributeError):
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.open()


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


