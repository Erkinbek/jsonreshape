"""The side-by-side diff of one value pair (docs/ARCHITECTURE.md §14.9 "Compare"; design:
scratchpad ``s5/map_repair_diff.md`` §4.7).

:class:`DiffView` shows ONE :class:`~jsonreshape.gui.vgrid.VirtualGrid` tree with the columns
*Kalit | A | B* (:class:`~jsonreshape.gui.difftree.DiffTreeModel`): A and B side by side, rows
tinted by their status through theme roles (``diff_added`` / ``diff_removed`` /
``diff_changed`` / ``diff_moved``; the gutter repeats it as ``+`` / ``−`` / ``~`` / ``↕`` so
colour is not the only cue), character highlights inside short changed strings and numbers,
unchanged subtrees collapsed into one row. Toolbar: array matching (Avto / Indeks / Tartibsiz /
Kalit: [field ▾]), "numbers as written", "Faqat farqlar", ◀ ▶ previous / next difference
(Shift+F7 / F7), expand / collapse all, swap A ⇄ B, and "Matn": two synchronized read-only
editors with the pretty text of each side (at most :data:`TEXT_CAP` each) whose lines are
aligned (filler lines for the missing side) and coloured like the rows.

What is compared comes from a :class:`PairSource` (worker-thread loader): :class:`ValuePairSource`
(two parsed values); :mod:`jsonreshape.gui.comparewindow` adds the node and record sources.
Loading, the text pages and big layouts run in silent tasks owned by the view (a newer load
supersedes an older one; :meth:`DiffView.shutdown` detaches them). The GUI thread never parses
or compares values: it only formats the visible rows.

Keys: F7 / Shift+F7 (next / previous difference; only when the view is built with
``shortcuts=True`` -- a window holding the view can bind them itself), → / ← expand / collapse
(or go to the first child / the parent), Enter toggles, ``*`` expands everything with
differences.
"""

from __future__ import annotations

import hashlib
import logging
import weakref
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QKeyEvent, QKeySequence, QShortcut, QTextBlockFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import codec
from jsonreshape.diff import OP_DEL, OP_EQ, OP_PAIRS, DiffOptions, Omitted, diff_values
from jsonreshape.diff import align as diff_align
from jsonreshape.errors import Cancelled, JsonReshapeError
from jsonreshape.gui import theme
from jsonreshape.gui.difftree import ABSENT, UNKNOWN, DiffRow, DiffTreeModel, count_text
from jsonreshape.gui.vgrid import Cell, Column, VirtualGrid
from jsonreshape.gui.widgets import ElidedLabel, format_count, format_size, mono_font, set_clipboard_text
from jsonreshape.i18n import t
from jsonreshape.tasks import CancelToken, check

log = logging.getLogger(__name__)

__all__ = [
    "DiffView",
    "FULL_VALUE_MAX",
    "GUI_MAX_CHANGES",
    "MATCH_MODES",
    "NEXT_KEY",
    "PREV_KEY",
    "PairSource",
    "SideText",
    "TEXT_CAP",
    "ValuePairSource",
    "align_lines",
    "gui_options",
]

#: Values (raw JSON bytes per side) up to this size are loaded for the complete tree; bigger
#: ones show only the differences (changes-only model).
FULL_VALUE_MAX = 4 * 1024 * 1024
#: Changes kept per value pair in the GUI (beyond: a changes-only model with a notice).
GUI_MAX_CHANGES = 50_000
#: Pretty text per side in the "Matn" mode.
TEXT_CAP = 4 * 1024 * 1024
#: Changed lines coloured in the text mode at most (beyond: a notice).
TEXT_COLOR_MAX = 200_000
MATCH_MODES = ("auto", "index", "unordered", "key")
NEXT_KEY = "F7"
PREV_KEY = "Shift+F7"
GLYPHS = {"changed": "~", "added": "+", "removed": "−", "moved": "↕"}
KEY_APPLY_MS = 500           # a typed array-key field applies after this pause


# ==================================================================================
# sources (worker threads)
# ==================================================================================
@dataclass
class SideText:
    """The pretty text of one side for the text mode."""

    text: str
    truncated: bool = False
    size: int = 0               # bytes of the value (for the notice)


def gui_options(opts: DiffOptions | None) -> DiffOptions:
    """``opts`` with the GUI's change limit (:data:`GUI_MAX_CHANGES`)."""
    o = opts or DiffOptions()
    return DiffOptions(array_match=o.array_match, array_keys=tuple(o.array_keys), numbers=o.numbers,
                       max_changes=GUI_MAX_CHANGES)


def _pretty_value(v: Any) -> SideText:
    if v is ABSENT or v is UNKNOWN:
        return SideText("")
    data = codec.dumps(v, indent=2)
    size = len(codec.dumps(v))
    truncated = len(data) > TEXT_CAP
    return SideText(data[:TEXT_CAP].decode("utf-8", errors="ignore"), truncated, size)


class PairSource:
    """What a :class:`DiffView` compares. ``load`` and ``texts`` run in worker threads.

    ``a_name`` / ``b_name``: the side labels (column titles ``A: …`` / ``B: …``);
    ``can_swap``: :meth:`DiffView.swap` reloads with ``swapped=True``."""

    a_name: str = "A"
    b_name: str = "B"
    can_swap: bool = True

    def names(self, swapped: bool) -> tuple[str, str]:
        return (self.b_name, self.a_name) if swapped else (self.a_name, self.b_name)

    def load(self, opts: DiffOptions, swapped: bool, cancel: CancelToken) -> DiffTreeModel:
        raise NotImplementedError

    def texts(self, swapped: bool, cancel: CancelToken) -> tuple[SideText, SideText]:
        raise NotImplementedError

    def close(self) -> None:
        """Release what the source holds (GUI thread)."""


class ValuePairSource(PairSource):
    """Two parsed values (``codec`` values)."""

    def __init__(self, a: Any, b: Any, *, a_name: str = "A", b_name: str = "B", root_label: str = "$") -> None:
        self.a = a
        self.b = b
        self.a_name = a_name
        self.b_name = b_name
        self.root_label = root_label

    def load(self, opts: DiffOptions, swapped: bool, cancel: CancelToken) -> DiffTreeModel:
        a, b = (self.b, self.a) if swapped else (self.a, self.b)
        vd = diff_values(a, b, gui_options(opts))
        check(cancel)
        return DiffTreeModel(a, b, vd, opts=opts, root_label=self.root_label)

    def texts(self, swapped: bool, cancel: CancelToken) -> tuple[SideText, SideText]:
        a, b = (self.b, self.a) if swapped else (self.a, self.b)
        return _pretty_value(a), _pretty_value(b)


# ==================================================================================
# text alignment (worker threads)
# ==================================================================================
def _line_hash(line: str) -> int:
    return int.from_bytes(hashlib.blake2b(line.encode("utf-8", "surrogatepass"), digest_size=8).digest(), "little")


def align_lines(a_text: str, b_text: str, cancel: CancelToken | None = None) -> tuple[list[str], list[str], list[str]]:
    """Two texts aligned line by line: ``(A lines, B lines, statuses)`` of equal length --
    ``equal`` / ``changed`` (a pair of differing lines) / ``removed`` (A only; B gets a filler
    line) / ``added`` (B only; A gets a filler) / the filler lines are empty strings. The
    alignment is the structural diff's sequence alignment (common prefix / suffix,
    ``difflib.SequenceMatcher`` on small parts, patience anchors on big ones; cancellable)."""
    la = a_text.split("\n") if a_text else []
    lb = b_text.split("\n") if b_text else []
    ha = [_line_hash(x) for x in la]
    check(cancel)
    hb = [_line_hash(x) for x in lb]
    check(cancel)
    # the structural diff's public sequence alignment: bounded memory and time, cancellable
    ops = diff_align(ha, hb, cancel)
    out_a: list[str] = []
    out_b: list[str] = []
    status: list[str] = []
    for op in ops:
        tag = op[0]
        if tag == OP_EQ or tag == OP_PAIRS:
            _, i, j, n = op
            out_a.extend(la[i:i + n])
            out_b.extend(lb[j:j + n])
            status.extend(["equal" if tag == OP_EQ else "changed"] * n)
        elif tag == OP_DEL:
            _, i, n = op
            out_a.extend(la[i:i + n])
            out_b.extend([""] * n)
            status.extend(["removed"] * n)
        else:                   # OP_INS
            _, j, n = op
            out_a.extend([""] * n)
            out_b.extend(lb[j:j + n])
            status.extend(["added"] * n)
    return out_a, out_b, status


@dataclass
class _TextResult:
    a: SideText
    b: SideText
    lines_a: list[str]
    lines_b: list[str]
    status: list[str]
    starts: list[int] = field(default_factory=list)     # first line of every run of differing lines

    def __post_init__(self) -> None:
        st = self.status
        self.starts = [i for i in range(len(st)) if st[i] != "equal" and (i == 0 or st[i - 1] == "equal")]


# ==================================================================================
# the grid source
# ==================================================================================
class _Rows:
    """GridSource over a :class:`DiffTreeModel`."""

    def __init__(self, view: DiffView) -> None:
        self.view = view

    def row_count(self) -> int:
        m = self.view._model
        return m.row_count() if m is not None else 0

    def _row(self, row: int) -> DiffRow | None:
        m = self.view._model
        if m is None or row < 0 or row >= m.row_count():
            return None
        return m.row(row)

    def cell(self, row: int, column: Column) -> Cell | None:
        r = self._row(row)
        if r is None:
            return None
        cid = column.id
        if cid == "key":
            expander = r.expander
            if expander == 1 and self.view._pending_layout(r):
                expander = 3
            if r.gap:
                return Cell(text=r.key_label, role="muted", depth=r.depth, italic=True)
            return Cell(text=r.key_label, role="key", badge=r.a_kind or r.b_kind, depth=r.depth, expander=expander,
                        bold=r.status == "contains" or r.point, tooltip=self.view.row_tooltip(r))
        if r.gap:
            return Cell()
        if cid == "a":
            return self._value_cell(r.a_text, r.a_kind, r.a_spans, r.summary)
        return self._value_cell(r.b_text, r.b_kind, r.b_spans, r.summary)

    @staticmethod
    def _value_cell(text: str, kind: str | None, spans: tuple, summary: bool) -> Cell:
        if summary:
            return Cell(text=text, role="muted", italic=True)
        if kind in ("object", "array"):            # a container summary: secondary text
            return Cell(text=text, role="muted", spans=spans)
        return Cell(text=text, role=kind if kind in ("string", "number", "bool", "null") else None, spans=spans)

    def rows_shown(self, first: int, last: int) -> None:
        return None

    def row_label(self, row: int) -> str | None:
        r = self._row(row)
        if r is None or not r.point:
            return "•" if r is not None and r.status == "contains" else None
        return GLYPHS.get(r.status)

    def row_background(self, row: int) -> str | None:
        r = self._row(row)
        return r.role if r is not None else None


# ==================================================================================
# the text pages
# ==================================================================================
class _TextSide(QPlainTextEdit):
    """One read-only side of the text mode."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(mono_font())


# ==================================================================================
# the view
# ==================================================================================
class DiffView(QWidget):
    """Side-by-side diff of one value pair (see the module docstring).

    Signals: ``loaded()`` (a model, an error or nothing is shown), ``current_changed(row)``
    (:class:`~jsonreshape.gui.difftree.DiffRow` or ``None``), ``reveal_requested(side, row)``
    ("a" / "b": the owner shows that node in its document), ``swap_requested()`` (a source that
    cannot swap: the owner swaps), ``options_changed(opts)``.

    ``app``: the :class:`~jsonreshape.gui.context.AppContext` (task manager);
    ``shortcuts``: install F7 / Shift+F7 on the view (``False`` when the window binds them);
    ``reveal``: a callable ``(side, row) -> bool`` telling whether "show in tree" is possible."""

    loaded = Signal()
    current_changed = Signal(object)
    reveal_requested = Signal(str, object)
    swap_requested = Signal()
    options_changed = Signal(object)

    def __init__(self, app: Any, parent: QWidget | None = None, *, shortcuts: bool = True,
                 reveal: Callable[[str, DiffRow], bool] | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("diff_view")
        self.app = app
        self._source: PairSource | None = None
        self._model: DiffTreeModel | None = None
        self._opts = DiffOptions()
        self._swapped = False
        self._gen = 0
        self._loading = False
        self._error: BaseException | None = None
        self._notice = ""
        self._text_gen = -1                 # load generation the text page shows
        self._text_task: Any = None
        self._text_result: _TextResult | None = None
        self._jobs: dict[tuple, Any] = {}   # ident -> running layout task
        self._can_reveal = reveal
        self._match_locked = False
        self._syncing = False
        self._only_user = False             # the user's "only differences" choice (a big pair forces it)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(self._build_toolbar())

        self.status_label = ElidedLabel("", self, mode=Qt.TextElideMode.ElideRight)
        self.status_label.setObjectName("diff_status")
        layout.addWidget(self.status_label)

        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack, 1)
        self.grid = VirtualGrid(self.stack)
        self.grid.setObjectName("diff_grid")
        self.grid.set_selection_mode("single")
        self.grid.set_row_labels(True)
        self.grid.set_alternating(False)
        self.grid.set_columns([
            Column("key", t("gui.diff.col.key"), 240),
            Column("a", t("gui.diff.col.a", name="A"), 360),
            Column("b", t("gui.diff.col.b", name="B"), 360),
        ])
        self.grid.set_tree_column(0)
        self.grid.set_placeholder_text(t("gui.diff.empty"))
        self._rows = _Rows(self)
        self.grid.set_source(self._rows)
        self.grid.set_key_handler(self._on_key)
        self.grid.expander_clicked.connect(self._on_expander)
        self.grid.activated.connect(lambda row, _col: self.toggle(int(row)))
        self.grid.current_changed.connect(self._on_current)
        self.grid.context_menu_requested.connect(self._on_context_menu)
        self.stack.addWidget(self.grid)
        self.stack.addWidget(self._build_text_page())

        if shortcuts:
            for seq, fn in ((NEXT_KEY, self.next_change), (PREV_KEY, self.prev_change)):
                sc = QShortcut(QKeySequence(seq), self)
                sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                sc.activated.connect(fn)
        self._key_timer = QTimer(self)
        self._key_timer.setSingleShot(True)
        self._key_timer.setInterval(KEY_APPLY_MS)
        self._key_timer.timeout.connect(self._apply_match)
        theme.notifier().theme_changed.connect(self._on_theme_changed)
        # a view deleted without shutdown() (its window closed) never gets a callback again
        self.destroyed.connect(lambda *_a, tasks=getattr(app, "tasks", None), ref=weakref.ref(self):
                               _detach(tasks, ref))
        self._sync_toolbar()

    # ------------------------------------------------------------------ construction
    def _tool(self, text: str, tip: str, fn: Callable[[], Any], name: str, *, checkable: bool = False) -> QToolButton:
        b = QToolButton(self)
        b.setText(text)
        b.setToolTip(tip)
        b.setAccessibleName(tip)
        b.setObjectName(name)
        b.setCheckable(checkable)
        b.setAutoRaise(True)
        if checkable:
            b.toggled.connect(lambda on: fn(on))   # type: ignore[call-arg]
        else:
            b.clicked.connect(lambda _checked=False: fn())
        return b

    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(4)
        self.match_label = QLabel(t("gui.diff.match.label"), self)
        bar.addWidget(self.match_label)
        self.match_combo = QComboBox(self)
        self.match_combo.setObjectName("diff_match")
        for mode in MATCH_MODES:
            self.match_combo.addItem(t(f"gui.diff.match.{mode}"), mode)
            self.match_combo.setItemData(self.match_combo.count() - 1, t(f"gui.diff.match.{mode}.tip"),
                                         Qt.ItemDataRole.ToolTipRole)
        self.match_label.setBuddy(self.match_combo)
        self.match_combo.activated.connect(lambda _i: self._on_match_changed())
        bar.addWidget(self.match_combo)
        self.key_combo = QComboBox(self)
        self.key_combo.setObjectName("diff_match_key")
        self.key_combo.setEditable(True)
        self.key_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.key_combo.setMinimumContentsLength(10)
        self.key_combo.setToolTip(t("gui.diff.match.key.tip"))
        if self.key_combo.lineEdit() is not None:
            self.key_combo.lineEdit().setPlaceholderText(t("gui.diff.match.key.placeholder"))
            self.key_combo.lineEdit().textEdited.connect(lambda _s: self._key_timer.start())
            self.key_combo.lineEdit().returnPressed.connect(self._apply_match)
        self.key_combo.activated.connect(lambda _i: self._apply_match())
        bar.addWidget(self.key_combo)
        self.numbers_check = QCheckBox(t("gui.diff.numbers_text"), self)
        self.numbers_check.setObjectName("diff_numbers_text")
        self.numbers_check.setToolTip(t("gui.diff.numbers_text.tip"))
        self.numbers_check.toggled.connect(lambda _on: self._apply_match())
        bar.addWidget(self.numbers_check)
        self.only_check = QCheckBox(t("gui.diff.only_changes"), self)
        self.only_check.setObjectName("diff_only_changes")
        self.only_check.setToolTip(t("gui.diff.only_changes.tip"))
        self.only_check.toggled.connect(self.set_only_changes)
        bar.addWidget(self.only_check)
        bar.addStretch(1)
        self.prev_button = self._tool("◀", t("gui.diff.prev.tip", key=PREV_KEY), self.prev_change, "diff_prev")
        self.next_button = self._tool("▶", t("gui.diff.next.tip", key=NEXT_KEY), self.next_change, "diff_next")
        self.expand_button = self._tool("⊞", t("gui.diff.expand_all.tip"), self.expand_all, "diff_expand_all")
        self.collapse_button = self._tool("⊟", t("gui.diff.collapse_all.tip"), self.collapse_all, "diff_collapse_all")
        self.swap_button = self._tool("A ⇄ B", t("gui.diff.swap.tip"), self.swap, "diff_swap")
        self.text_button = self._tool(t("gui.diff.text_mode"), t("gui.diff.text_mode.tip"), self.set_text_mode,
                                      "diff_text_mode", checkable=True)
        for b in (self.prev_button, self.next_button, self.expand_button, self.collapse_button, self.swap_button,
                  self.text_button):
            bar.addWidget(b)
        return bar

    def _build_text_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.text_status = ElidedLabel("", page, mode=Qt.TextElideMode.ElideRight)
        self.text_status.setObjectName("diff_text_status")
        layout.addWidget(self.text_status)
        split = QSplitter(Qt.Orientation.Horizontal, page)
        self.text_a = _TextSide(split)
        self.text_a.setObjectName("diff_text_a")
        self.text_b = _TextSide(split)
        self.text_b.setObjectName("diff_text_b")
        for ed in (self.text_a, self.text_b):
            ed.setPlaceholderText(t("gui.common.loading"))
        split.addWidget(self.text_a)
        split.addWidget(self.text_b)
        layout.addWidget(split, 1)
        self.text_a.verticalScrollBar().valueChanged.connect(lambda v: self._sync_scroll(self.text_b, v, True))
        self.text_b.verticalScrollBar().valueChanged.connect(lambda v: self._sync_scroll(self.text_a, v, True))
        self.text_a.horizontalScrollBar().valueChanged.connect(lambda v: self._sync_scroll(self.text_b, v, False))
        self.text_b.horizontalScrollBar().valueChanged.connect(lambda v: self._sync_scroll(self.text_a, v, False))
        return page

    def _sync_scroll(self, other: QPlainTextEdit, value: int, vertical: bool) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            bar = other.verticalScrollBar() if vertical else other.horizontalScrollBar()
            bar.setValue(value)
        finally:
            self._syncing = False

    # ------------------------------------------------------------------ public state
    def model(self) -> DiffTreeModel | None:
        return self._model

    def source(self) -> PairSource | None:
        return self._source

    def options(self) -> DiffOptions:
        return self._opts

    def swapped(self) -> bool:
        return self._swapped

    def is_loading(self) -> bool:
        return self._loading

    def error(self) -> BaseException | None:
        return self._error

    def message(self) -> str:
        """The status line (counts, notices, errors)."""
        return self.status_label.full_text()

    def text_mode(self) -> bool:
        return self.stack.currentIndex() == 1

    def text_editors(self) -> tuple[QPlainTextEdit, QPlainTextEdit]:
        return self.text_a, self.text_b

    def text_ready(self) -> bool:
        """The text page shows the texts of the current load."""
        return self._text_result is not None and self._text_gen == self._gen

    # ------------------------------------------------------------------ loading
    def set_source(self, source: PairSource | None, *, opts: DiffOptions | None = None,
                   swapped: bool = False) -> None:
        """Compare what ``source`` gives (``None``: show nothing); ``opts`` replaces the options."""
        old = self._source
        self._source = source
        self._swapped = bool(swapped)
        if opts is not None:
            self._opts = opts
            self._show_options(opts)
        if old is not None and old is not source:
            try:
                old.close()
            except Exception:   # pragma: no cover - best effort
                log.debug("closing a pair source failed", exc_info=True)
        self.reload()

    def set_options(self, opts: DiffOptions, *, reload: bool = True) -> None:
        """Use ``opts`` (array matching, numbers) and compare again."""
        self._opts = opts
        self._show_options(opts)
        if reload:
            self.reload()

    def set_match_locked(self, locked: bool, tip: str | None = None) -> None:
        """Disable the options controls (the options belong to the owner, e.g. a file diff)."""
        self._match_locked = bool(locked)
        for w in (self.match_combo, self.key_combo, self.numbers_check, self.match_label):
            w.setEnabled(not locked)
            if tip:
                w.setToolTip(tip)
        self._sync_toolbar()

    def reload(self) -> None:
        """Compare again (a silent task owned by the view; an older load is superseded)."""
        self._gen += 1
        gen = self._gen
        self._cancel_tasks()
        self._model = None
        self._error = None
        self._text_result = None
        self._text_gen = -1
        source = self._source
        self._refresh_grid(reset=True)
        self._set_titles()
        names = source.names(self._swapped) if source is not None else ("A", "B")
        if source is None:
            self._loading = False
            self._set_status("")
            self._sync_toolbar()
            self.loaded.emit()
            return
        self._loading = True
        self._set_status(t("gui.diff.loading"))
        self.grid.set_placeholder_text(t("gui.diff.loading"))
        self._sync_toolbar()
        opts, swapped = self._opts, self._swapped

        def work(ctx: Any) -> DiffTreeModel:
            model = source.load(opts, swapped, ctx.cancel)
            model.array_fields()                # (scanned here, not in the GUI thread)
            return model

        self.app.tasks.run(work, title=t("gui.diff.task", a=names[0], b=names[1]), kind="compare", owner=self,
                           silent=True, on_done=lambda m: self._loaded(gen, m),
                           on_error=lambda exc: self._failed(gen, exc), on_cancel=lambda: self._cancelled(gen))
        if self.text_mode():
            self._start_texts()

    def _cancel_tasks(self) -> None:
        tasks = getattr(self.app, "tasks", None)
        if tasks is None:
            return
        for task in tasks.tasks(owner=self, include_silent=True):
            task.cancel()
        self._jobs.clear()
        self._text_task = None

    def _set_titles(self) -> None:
        source = self._source
        names = source.names(self._swapped) if source is not None else ("A", "B")
        cols = self.grid.columns()
        cols[1].title = t("gui.diff.col.a", name=names[0])
        cols[2].title = t("gui.diff.col.b", name=names[1])
        self.grid.refresh_columns()

    def _loaded(self, gen: int, model: DiffTreeModel) -> None:
        if gen != self._gen:
            return
        self._loading = False
        self._model = model
        self._set_titles()                  # a source may know its names only after loading
        if self._only_user or not model.can_show_all:
            model.set_only_changes(True)
        self.grid.set_placeholder_text(t("gui.diff.empty"))
        self._refresh_grid(reset=True)
        first = model.first_change()
        self._refresh_grid(reset=False)
        self.grid.set_current(first if first is not None else 0, 0, scroll="center")
        self._update_status()
        self._sync_toolbar()
        self._fill_key_fields(model)
        self.loaded.emit()

    def _failed(self, gen: int, exc: BaseException) -> None:
        if gen != self._gen:
            return
        self._loading = False
        self._error = exc
        text = exc.message if isinstance(exc, JsonReshapeError) else t("gui.diff.unexpected")
        if not isinstance(exc, JsonReshapeError):
            log.error("comparing failed", exc_info=(type(exc), exc, exc.__traceback__))
        self._set_status(t("gui.diff.failed", error=text))
        self.grid.set_placeholder_text(t("gui.diff.failed", error=text))
        self._refresh_grid(reset=True)
        self._sync_toolbar()
        self.loaded.emit()

    def _cancelled(self, gen: int) -> None:
        if gen != self._gen:
            return
        self._loading = False
        self._set_status(t("gui.diff.cancelled"))
        self.grid.set_placeholder_text(t("gui.diff.cancelled"))
        self._sync_toolbar()
        self.loaded.emit()

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _update_status(self) -> None:
        m = self._model
        if m is None:
            return
        parts = [m.describe()]
        if m.truncated:
            parts.append(t("gui.diff.truncated", shown=format_count(len(m.vd.changes)),
                           total=format_count(m.total_changes)))
        elif not m.can_show_all and not m.equal:
            parts.append(t("gui.diff.changes_only"))
        if self._notice:
            parts.append(self._notice)
        self._set_status(" · ".join(parts))

    def _fill_key_fields(self, model: DiffTreeModel) -> None:
        current = self.key_combo.currentText()
        fields = model.array_fields()
        self.key_combo.blockSignals(True)
        try:
            self.key_combo.clear()
            self.key_combo.addItems(fields)
            if current:
                self.key_combo.setEditText(current)
            elif "id" in fields:
                self.key_combo.setEditText("id")
            else:
                self.key_combo.setEditText("")
        finally:
            self.key_combo.blockSignals(False)

    # ------------------------------------------------------------------ options
    def _show_options(self, opts: DiffOptions) -> None:
        mode = "key" if opts.array_keys else opts.array_match
        if mode not in MATCH_MODES:
            mode = "auto"
        self.match_combo.blockSignals(True)
        self.numbers_check.blockSignals(True)
        try:
            self.match_combo.setCurrentIndex(MATCH_MODES.index(mode))
            if opts.array_keys:
                pat, fld = opts.array_keys[0]
                text = fld if pat is None else f"{pat.text}={fld}"
                self.key_combo.setEditText(text)
            self.numbers_check.setChecked(opts.numbers == "text")
        finally:
            self.match_combo.blockSignals(False)
            self.numbers_check.blockSignals(False)
        self._sync_toolbar()

    def _on_match_changed(self) -> None:
        self._sync_toolbar()
        if self.match_combo.currentData() == "key" and not self.key_combo.currentText().strip():
            self.key_combo.setFocus()
            self._notice = t("gui.diff.match.key_needed")
            self._update_status() if self._model is not None else self._set_status(self._notice)
            return
        self._apply_match()

    def _options_from_controls(self) -> DiffOptions:
        mode = self.match_combo.currentData() or "auto"
        numbers = "text" if self.numbers_check.isChecked() else "value"
        if mode == "key":
            key = self.key_combo.currentText().strip()
            return DiffOptions(array_match="key", array_keys=(key,), numbers=numbers)
        return DiffOptions(array_match=mode, numbers=numbers)

    def _apply_match(self) -> None:
        self._key_timer.stop()
        if self._match_locked:
            return
        if self.match_combo.currentData() == "key" and not self.key_combo.currentText().strip():
            return
        try:
            opts = self._options_from_controls()
        except JsonReshapeError as exc:
            self._notice = exc.message
            self._set_status(exc.message)
            return
        self._notice = ""
        if _same_options(opts, self._opts):
            return
        self._opts = opts
        self.options_changed.emit(opts)
        self.reload()

    # ------------------------------------------------------------------ rows / grid
    def _refresh_grid(self, *, reset: bool) -> None:
        if reset:
            self.grid.reset()
        else:
            self.grid.rows_changed(keep_current=False)
        self.grid.refresh(request=True)

    def _restructured(self, select: int | None) -> None:
        self._refresh_grid(reset=False)
        if select is not None and select >= 0:
            self.grid.set_current(select, 0, scroll="ensure")

    def current_row(self) -> DiffRow | None:
        m = self._model
        row = self.grid.current()[0]
        if m is None or row < 0 or row >= m.row_count():
            return None
        return m.row(row)

    def select_row(self, row: int, *, scroll: str = "center") -> None:
        self.grid.set_current(row, 0, scroll=scroll)

    def _on_current(self, row: Any, _col: int) -> None:
        m = self._model
        r = None
        if m is not None and row is not None and 0 <= int(row) < m.row_count():
            r = m.row(int(row))
        self.current_changed.emit(r)

    def row_tooltip(self, r: DiffRow) -> str:
        parts = [t(f"gui.diff.status.{r.status}")]
        if r.a_path is not None and r.b_path is not None and r.a_path != r.b_path:
            parts.append(t("gui.diff.tip.paths", a=_path_text(r.a_path), b=_path_text(r.b_path)))
        elif r.path is not None:
            parts.append(_path_text(r.path))
        if r.status == "contains" and r.changes:
            parts.append(count_text("gui.diff.tip.changes", r.changes))
        if r.status == "changed" and r.a_kind != r.b_kind and r.a_kind and r.b_kind:
            parts.append(t("gui.diff.tip.kinds", a=r.a_kind, b=r.b_kind))
        m = self._model
        if m is not None:
            a, b = m.values_at(r.row)
            for side, v in (("A", a), ("B", b)):
                if isinstance(v, Omitted) and v.preview:
                    parts.append(f"{side}: {v.preview}")
        return "\n".join(parts)

    def _pending_layout(self, r: DiffRow) -> bool:
        m = self._model
        return bool(self._jobs) and m is not None and m.ident(r.row) in self._jobs

    def toggle(self, row: int) -> None:
        """Expand / collapse ``row`` (a gap row: switch "only differences" off)."""
        m = self._model
        if m is None or row < 0 or row >= m.row_count():
            return
        r = m.row(row)
        if r.gap:
            self.only_check.setChecked(False)
            return
        if m.is_expanded(row):
            m.collapse(row)
            self._restructured(row)
            return
        self.expand_row(row)

    def expand_row(self, row: int) -> None:
        m = self._model
        if m is None:
            return
        added = m.expand(row)
        if added == -1:
            self._start_layout_job(row)
            self.grid.refresh()
            return
        if added:
            self._restructured(row)

    def collapse_row(self, row: int) -> None:
        m = self._model
        if m is not None and m.collapse(row):
            self._restructured(row)

    def _start_layout_job(self, row: int) -> None:
        m = self._model
        if m is None:
            return
        job = m.layout_job(row)
        if job is None:
            return
        ident = job.ident
        if ident in self._jobs:
            return
        gen = self._gen

        def done(result: Any) -> None:
            self._jobs.pop(ident, None)
            if gen != self._gen or self._model is not m:
                return
            new_row = m.apply_job(result)
            self._restructured(new_row)

        def failed(exc: BaseException) -> None:
            self._jobs.pop(ident, None)
            if not isinstance(exc, Cancelled):
                log.warning("laying out diff rows failed: %r", exc)
            self.grid.refresh()

        self._jobs[ident] = self.app.tasks.run(lambda ctx: job.run(ctx.cancel), title=t("gui.diff.task.layout"),
                                               kind="compare", owner=self, silent=True, on_done=done,
                                               on_error=failed, on_cancel=lambda: self._jobs.pop(ident, None))

    def _on_expander(self, row: Any) -> None:
        self.toggle(int(row))

    def _on_key(self, event: QKeyEvent) -> bool:
        m = self._model
        if m is None:
            return False
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                   | Qt.KeyboardModifier.MetaModifier | Qt.KeyboardModifier.ShiftModifier):
            return False
        key = event.key()
        row = self.grid.current()[0]
        if key == Qt.Key.Key_Asterisk:
            self.expand_all()
            return True
        if row < 0 or row >= m.row_count():
            return False
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggle(row)
            return True
        if key == Qt.Key.Key_Right:
            r = m.row(row)
            if r.expander == 1:
                self.expand_row(row)
            elif r.expander == 2 and row + 1 < m.row_count():
                self.grid.set_current(row + 1, 0)
            return True
        if key == Qt.Key.Key_Left:
            r = m.row(row)
            if r.expander == 2:
                self.collapse_row(row)
            else:
                kp = m.keypath(row)
                if len(kp) > 1:
                    parent = m.row_of_keypath(kp[:-1])
                    if parent is not None:
                        self.grid.set_current(parent, 0)
            return True
        return False

    # ------------------------------------------------------------------ navigation & modes
    def next_change(self) -> int | None:
        """Select the next difference (F7); its row or ``None`` (a status message)."""
        return self._go(True)

    def prev_change(self) -> int | None:
        """Select the previous difference (Shift+F7)."""
        return self._go(False)

    def _go(self, forward: bool) -> int | None:
        m = self._model
        if m is None:
            return None
        if self.text_mode():
            return self._go_text(forward)
        row = self.grid.current()[0]
        before = m.version
        target = m.next_change(row) if forward else m.prev_change(row if row >= 0 else m.row_count())
        if target is None:
            show = getattr(self.app, "show_status", None)
            if callable(show):
                show(t("gui.diff.no_more"), 3000)
            return None
        if m.version != before:
            self._refresh_grid(reset=False)
        self.grid.set_current(target, 0, scroll="center")
        return target

    def select_last_change(self) -> int | None:
        """Select the last difference (expanding its ancestors); its row or ``None``."""
        m = self._model
        if m is None:
            return None
        before = m.version
        row = m.prev_change(m.row_count())
        if row is None:
            return None
        if m.version != before:
            self._refresh_grid(reset=False)
        self.grid.set_current(row, 0, scroll="center")
        return row

    def set_only_changes(self, on: bool) -> None:
        """Show only the rows with differences (each run of equal siblings: one gap row)."""
        if self.only_check.isChecked() != bool(on):
            self.only_check.setChecked(bool(on))     # comes back here through the signal
            return
        m = self._model
        if m is None or m.can_show_all:
            self._only_user = bool(on)
        if m is None:
            return
        cur = self.current_row()
        ident = m.ident(cur.row) if cur is not None else None
        if m.set_only_changes(on):
            self._refresh_grid(reset=False)
            row = m.row_of_ident(ident) if ident is not None else None
            if row is None:
                row = m.first_change()
            self.grid.set_current(row if row is not None else 0, 0, scroll="center")
        self._sync_toolbar()

    def expand_all(self) -> None:
        m = self._model
        if m is None:
            return
        cur = self.current_row()
        ident = m.ident(cur.row) if cur is not None else None
        done = m.expand_all()
        self._notice = "" if done else t("gui.diff.capped")
        self._refresh_grid(reset=False)
        row = m.row_of_ident(ident) if ident is not None else None
        self.grid.set_current(row if row is not None else 0, 0, scroll="ensure")
        self._update_status()

    def collapse_all(self) -> None:
        m = self._model
        if m is None:
            return
        m.collapse_all()
        self._notice = ""
        self._refresh_grid(reset=False)
        self.grid.set_current(0, 0, scroll="top")
        self._update_status()

    def swap(self) -> None:
        """Swap A and B (reload with the sides exchanged, or ``swap_requested`` for a source that
        cannot swap)."""
        src = self._source
        if src is None:
            return
        if not src.can_swap:
            self.swap_requested.emit()
            return
        self._swapped = not self._swapped
        self.reload()

    def set_text_mode(self, on: bool) -> None:
        """Show the two texts line by line (``on``) or the tree."""
        on = bool(on)
        if self.text_button.isChecked() != on:
            self.text_button.setChecked(on)        # comes back here through the signal
            return
        self.stack.setCurrentIndex(1 if on else 0)
        if on and not self.text_ready():
            self._start_texts()
        self._sync_toolbar()

    def _sync_toolbar(self) -> None:
        m = self._model
        has = m is not None
        mode = self.match_combo.currentData()
        self.key_combo.setVisible(mode == "key")
        self.key_combo.setEnabled(mode == "key" and not self._match_locked)
        forced = has and not m.can_show_all
        self.only_check.setEnabled(has and not forced)
        self.only_check.blockSignals(True)
        self.only_check.setChecked(True if forced else self._only_user)
        self.only_check.blockSignals(False)
        self.only_check.setToolTip(t("gui.diff.only_changes.forced" if forced else "gui.diff.only_changes.tip"))
        nav = has and not m.equal
        self.prev_button.setEnabled(nav)
        self.next_button.setEnabled(nav)
        tree = not self.text_mode()
        self.expand_button.setEnabled(has and tree)
        self.collapse_button.setEnabled(has and tree)
        self.swap_button.setEnabled(self._source is not None)
        self.text_button.setEnabled(self._source is not None)

    # ------------------------------------------------------------------ context menu
    def _on_context_menu(self, row: Any, _col: int, pos: QPoint) -> None:
        m = self._model
        if m is None or row is None or int(row) < 0:
            return
        menu = self.build_menu(int(row))
        if menu is not None:
            menu.aboutToHide.connect(menu.deleteLater)
            menu.popup(pos)

    def build_menu(self, row: int) -> QMenu | None:
        """The context menu of ``row`` (copy the values / the path, show in the tree)."""
        m = self._model
        if m is None or row < 0 or row >= m.row_count():
            return None
        r = m.row(row)
        if r.gap:
            return None
        a, b = m.values_at(row)
        menu = QMenu(self)
        menu.setObjectName("diff_menu")
        menu.setToolTipsVisible(True)
        for side, v, key in (("a", a, "gui.diff.menu.copy_a"), ("b", b, "gui.diff.menu.copy_b")):
            act = menu.addAction(t(key))
            act.setObjectName(f"diff_copy_{side}")
            ok = v is not ABSENT and v is not UNKNOWN and not isinstance(v, Omitted)
            act.setEnabled(ok)
            act.triggered.connect(lambda _c=False, val=v: self.copy_value(val))
        act = menu.addAction(t("gui.diff.menu.copy_path"))
        act.setObjectName("diff_copy_path")
        act.setEnabled(r.path is not None)
        act.triggered.connect(lambda _c=False, text=r.path_text: set_clipboard_text(text))
        menu.addSeparator()
        for side, path in (("a", r.a_path), ("b", r.b_path)):
            act = menu.addAction(t(f"gui.diff.menu.reveal_{side}"))
            act.setObjectName(f"diff_reveal_{side}")
            ok = path is not None and (self._can_reveal is None or bool(self._can_reveal(side, r)))
            act.setEnabled(ok)
            act.triggered.connect(lambda _c=False, s=side, rr=r: self.reveal_requested.emit(s, rr))
        return menu

    def copy_value(self, v: Any) -> Any:
        """Copy a value as pretty JSON (exact numbers); the text is built in a silent task
        (a value may hold megabytes). Returns the task."""
        def work(_ctx: Any) -> str:
            return codec.dumps(v, indent=2).decode("utf-8", errors="replace")

        return self.app.tasks.run(work, title=t("gui.diff.task.copy"), kind="value", owner=self, silent=True,
                                  on_done=set_clipboard_text)

    # ------------------------------------------------------------------ text mode
    def _start_texts(self) -> None:
        source = self._source
        if source is None:
            return
        gen = self._gen
        swapped = self._swapped
        self.text_status.setText(t("gui.diff.loading"))
        for ed in (self.text_a, self.text_b):
            ed.clear()
            ed.setPlaceholderText(t("gui.common.loading"))

        def work(ctx: Any) -> _TextResult:
            a, b = source.texts(swapped, ctx.cancel)
            la, lb, st = align_lines(a.text, b.text, ctx.cancel)
            return _TextResult(a, b, la, lb, st)

        self._text_task = self.app.tasks.run(work, title=t("gui.diff.task.text"), kind="compare", owner=self,
                                             silent=True, on_done=lambda res: self._texts_done(gen, res),
                                             on_error=lambda exc: self._texts_failed(gen, exc))

    def _texts_done(self, gen: int, res: _TextResult) -> None:
        if gen != self._gen:
            return
        self._text_task = None
        self._text_result = res
        self._text_gen = gen
        self.text_a.setPlainText("\n".join(res.lines_a))
        self.text_b.setPlainText("\n".join(res.lines_b))
        self._color_lines()
        changed = sum(1 for s in res.status if s != "equal")
        notes = [count_text("gui.diff.text.lines", changed) if changed else t("gui.diff.text.same")]
        names = self._source.names(self._swapped) if self._source is not None else ("A", "B")
        for side, st in ((names[0], res.a), (names[1], res.b)):
            if st.truncated:
                notes.append(t("gui.diff.text.truncated", side=side, size=format_size(TEXT_CAP),
                               total=format_size(st.size)))
        if changed > TEXT_COLOR_MAX:
            notes.append(t("gui.diff.text.uncolored", count=format_count(TEXT_COLOR_MAX)))
        self.text_status.setText(" · ".join(notes))

    def _texts_failed(self, gen: int, exc: BaseException) -> None:
        if gen != self._gen:
            return
        self._text_task = None
        if isinstance(exc, Cancelled):
            return
        text = exc.message if isinstance(exc, JsonReshapeError) else t("gui.diff.unexpected")
        self.text_status.setText(t("gui.diff.failed", error=text))
        for ed in (self.text_a, self.text_b):
            ed.setPlaceholderText("")

    def _color_lines(self) -> None:
        res = self._text_result
        if res is None:
            return
        colors = {s: QColor(theme.color(role)) for s, role in (("changed", "diff_changed"), ("added", "diff_added"),
                                                                ("removed", "diff_removed"))}
        filler = QBrush(QColor(theme.color("line")), Qt.BrushStyle.BDiagPattern)
        plain = QTextBlockFormat()
        # runs of equal statuses (one block-format change per run)
        runs: list[tuple[int, int, str]] = []
        start = 0
        status = res.status
        for i in range(1, len(status) + 1):
            if i == len(status) or status[i] != status[start]:
                if status[start] != "equal":
                    runs.append((start, i - 1, status[start]))
                start = i
        colored = 0
        for ed, side in ((self.text_a, "a"), (self.text_b, "b")):
            doc = ed.document()
            cur = QTextCursor(doc)
            cur.select(QTextCursor.SelectionType.Document)
            cur.setBlockFormat(plain)
            colored = 0
            for first, last, st in runs:
                if colored > TEXT_COLOR_MAX:
                    break
                colored += last - first + 1
                fmt = QTextBlockFormat()
                missing = (st == "added" and side == "a") or (st == "removed" and side == "b")
                fmt.setBackground(filler if missing else QBrush(colors[st]))
                b0 = doc.findBlockByNumber(first)
                b1 = doc.findBlockByNumber(last)
                if not b0.isValid() or not b1.isValid():
                    continue
                c = QTextCursor(b0)
                c.setPosition(b1.position(), QTextCursor.MoveMode.KeepAnchor)
                c.setBlockFormat(fmt)
            ed.moveCursor(QTextCursor.MoveOperation.Start)

    def line_statuses(self) -> list[str]:
        """The statuses of the aligned text lines (tests)."""
        return list(self._text_result.status) if self._text_result is not None else []

    def _go_text(self, forward: bool) -> int | None:
        """Move both editors to the first line of the next / previous run of differing lines."""
        res = self._text_result
        if res is None or not res.starts:
            return None
        starts = res.starts
        line = self.text_a.textCursor().blockNumber()
        k = bisect_right(starts, line)            # runs starting at or before the line: starts[:k]
        if forward:
            if k >= len(starts):
                return None
            found = starts[k]
        else:
            # at the first line of a run: the run before; anywhere else: the run starting before
            here = k - 1
            if here >= 0 and starts[here] == line:
                here -= 1
            if here < 0:
                return None
            found = starts[here]
        for ed in (self.text_a, self.text_b):
            block = ed.document().findBlockByNumber(found)
            cur = QTextCursor(block)
            ed.setTextCursor(cur)
            ed.centerCursor()
        return found

    def _on_theme_changed(self) -> None:
        try:
            if self._text_result is not None:
                self._color_lines()
        except RuntimeError:          # pragma: no cover - deleted widget
            pass

    # ------------------------------------------------------------------ lifetime
    def shutdown(self) -> None:
        """Detach the view's tasks (the owner closes)."""
        self._gen += 1
        try:
            self.app.tasks.detach(self)
        except Exception:     # pragma: no cover - best effort
            log.debug("detaching the diff view's tasks failed", exc_info=True)
        self._jobs.clear()
        try:
            theme.notifier().theme_changed.disconnect(self._on_theme_changed)
        except (RuntimeError, TypeError):
            pass
        src, self._source = self._source, None
        if src is not None:
            try:
                src.close()
            except Exception:   # pragma: no cover
                log.debug("closing a pair source failed", exc_info=True)
        self._model = None


def _detach(tasks: Any, ref: Any) -> None:
    owner = ref()
    if tasks is not None and owner is not None:
        try:
            tasks.detach(owner)
        except Exception:   # pragma: no cover - best effort at teardown
            log.debug("detach failed", exc_info=True)


def _path_text(path: tuple) -> str:
    from jsonreshape.paths import format_path

    return format_path(path) or "$"


def _same_options(a: DiffOptions, b: DiffOptions) -> bool:
    return (a.array_match == b.array_match and a.numbers == b.numbers
            and [(p.text if p is not None else None, f) for p, f in a.array_keys]
            == [(p.text if p is not None else None, f) for p, f in b.array_keys])

