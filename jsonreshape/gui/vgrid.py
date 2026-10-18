"""VirtualGrid: the virtual list / table / tree widget of the GUI (docs/ARCHITECTURE.md §11.5).

Qt item views keep per-row state and use ``int`` scroll ranges, so they cannot show 100 M
rows. :class:`VirtualGrid` keeps **no per-row state**: rows are Python ints (10**12 is
fine), every visible cell is asked from a :class:`GridSource` while painting, and the
selection is a short list of rectangles.

Coordinates and indices
-----------------------
* Column indices (signals, :meth:`VirtualGrid.current`, ranges, :meth:`~VirtualGrid.column_at`
  ...) are indices into :meth:`VirtualGrid.columns`: the full column list in *display
  order*. Dragging a header section reorders that list; hidden columns stay in it
  (``visible=False``), are not painted and are skipped by keyboard navigation.
* Ranges ``(row0, row1, col0, col1)`` are inclusive.
* Geometry (:meth:`~VirtualGrid.cell_rect`, :meth:`~VirtualGrid.row_at`, ...) is in viewport
  coordinates; the header shares the viewport's x axis.

Vertical scrolling is by whole rows. The scrollbar range is ``rows - page`` while it fits a
C ``int``, otherwise a scaled ``0..2**30`` range mapped proportionally to the top row;
wheel, keys and :meth:`~VirtualGrid.scroll_to` always move exact rows. Horizontal
scrolling is in pixels; the header and the frozen first column stay in place.

Deviation from §11.5: row-carrying signals use ``object`` for the row
(``current_changed = Signal(object, int)`` ...) because a Qt ``int`` signal argument
overflows above 2**31 - 1 rows.
"""

from __future__ import annotations

import logging
import math
import textwrap
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QContextMenuEvent,
    QCursor,
    QFont,
    QFontInfo,
    QFontMetrics,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
    QWheelEvent,
)
from PySide6.QtWidgets import QAbstractScrollArea, QAbstractSlider, QApplication, QToolTip, QWidget

from jsonreshape.gui import theme
from jsonreshape.i18n import t

log = logging.getLogger(__name__)

__all__ = ["Cell", "Column", "GridSource", "GridHeader", "VirtualGrid", "MIN_COLUMN_WIDTH", "SELECTION_MODES"]

SELECTION_MODES = ("single", "rows", "cells")
SCROLL_MODES = ("ensure", "center", "top", "bottom", "none")
MIN_COLUMN_WIDTH = 24
MAX_FIT_WIDTH = 1200          # double-click "fit" never makes a column wider than this
WHEEL_ROWS = 3                # rows per wheel notch
ELLIPSIS = "…"
PLACEHOLDER = ELLIPSIS        # painted for cells that are not loaded yet
_INT_MAX = 2**31 - 1
_SCALE = 2**30                # scaled scrollbar range for row counts beyond a C int
_Y_LIMIT = 1 << 24            # rects of rows far outside the viewport are clamped to this
_TIP_CHARS = 4000             # tooltips show at most this many characters
_TIP_WIDTH = 120              # ... wrapped at this many columns
_FIT_CHARS = 512              # characters measured per cell by "fit column"
_MIN_POINT_SIZE = 5.0
_MAX_POINT_SIZE = 64.0
#: (light, dark) share of the highlight color in the selection background with / without
#: focus, and in the outline of the current row / cell without focus.
SEL_BLEND = (0.22, 0.30)
SEL_INACTIVE_BLEND = (0.18, 0.22)
FOCUS_INACTIVE_BLEND = (0.45, 0.55)


# ---------------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------------
@dataclass(slots=True)
class Cell:
    """What the source returns for one visible cell."""

    text: str = ""
    role: str | None = None     # theme color role: key string number bool null object array muted error link ...
    badge: str | None = None    # type badge before the text: object array string number bool null
    depth: int = 0              # tree indent level (tree column only)
    expander: int = 0           # 0 none, 1 collapsed, 2 expanded, 3 loading
    spans: tuple = ()           # (start, end[, current]) highlight ranges in text; current -> "current_match"
    italic: bool = False
    bold: bool = False
    align: str = "left"         # left | right | center ("left" = the column's alignment)
    tooltip: str | None = None


@dataclass
class Column:
    """A grid column. The grid mutates ``width`` / ``visible`` (header gestures)."""

    id: str
    title: str
    width: int = 120
    visible: bool = True
    align: str = "left"
    sortable: bool = False
    sort_order: int = 0         # 0 none, 1 ascending, -1 descending
    sort_priority: int = 0      # 1-based when several sort keys
    tooltip: str | None = None  # header tooltip (default: the title when elided + sort state)


class GridSource(Protocol):
    """Data provider of a :class:`VirtualGrid`. ``rows_shown`` and ``row_label`` are optional."""

    def row_count(self) -> int: ...

    def cell(self, row: int, column: Column) -> Cell | None: ...      # None: not loaded yet (placeholder)

    def rows_shown(self, first: int, last: int) -> None: ...          # visible window (+1 page) changed

    def row_label(self, row: int) -> str | None: ...                  # gutter text ("#12")


# ---------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------
def _blend(a: QColor, b: QColor, f: float) -> QColor:
    return QColor(
        round(a.red() + (b.red() - a.red()) * f),
        round(a.green() + (b.green() - a.green()) * f),
        round(a.blue() + (b.blue() - a.blue()) * f),
    )


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


def _wrap_tip(text: str) -> str:
    if len(text) > _TIP_CHARS:
        text = text[:_TIP_CHARS] + ELLIPSIS
    lines = []
    for line in text.split("\n"):
        if len(line) <= _TIP_WIDTH:
            lines.append(line)
        else:
            lines.extend(textwrap.wrap(line, _TIP_WIDTH, replace_whitespace=False, drop_whitespace=False,
                                       break_long_words=True) or [""])
    return "\n".join(lines)


class _Colors:
    """Colors resolved once per palette/theme change (painting must not look them up)."""

    __slots__ = ("base", "alt", "text", "sel", "sel_inactive", "hover", "focus", "focus_inactive", "gutter",
                 "muted", "faint", "guide", "line", "match", "current_match", "roles", "header_bg", "header_text",
                 "header_hover", "accent", "accent_soft")

    def __init__(self, pal: QPalette) -> None:
        cr = QPalette.ColorRole
        self.base = pal.color(cr.Base)
        self.alt = pal.color(cr.AlternateBase)
        self.text = pal.color(cr.Text)
        highlight = pal.color(cr.Highlight)
        dark = self.base.lightness() < 128
        # the selection stays clearly visible without focus (a grid next to another pane); the
        # current row / cell then gets a softer outline than the focused one
        self.sel = _blend(self.base, highlight, SEL_BLEND[1] if dark else SEL_BLEND[0])
        self.sel_inactive = _blend(self.base, highlight, SEL_INACTIVE_BLEND[1] if dark else SEL_INACTIVE_BLEND[0])
        self.hover = _blend(self.base, highlight, 0.08 if dark else 0.06)
        self.focus = QColor(highlight)
        self.focus_inactive = _blend(self.base, highlight,
                                     FOCUS_INACTIVE_BLEND[1] if dark else FOCUS_INACTIVE_BLEND[0])
        self.gutter = _blend(self.base, pal.color(cr.Window), 0.7)
        self.muted = QColor(theme.color("muted"))
        self.faint = QColor(theme.color("faint"))
        self.guide = QColor(theme.color("guide"))
        self.line = QColor(theme.color("line"))
        self.match = QColor(theme.color("match"))
        self.current_match = QColor(theme.color("current_match"))
        self.accent = QColor(highlight)
        self.accent_soft = QColor(theme.color("accent_soft"))
        self.roles = {role: QColor(theme.color(role)) for role in theme.ROLES}
        self.header_bg = pal.color(cr.Button)
        self.header_text = pal.color(cr.ButtonText)
        self.header_hover = _blend(self.header_bg, self.header_text, 0.06)


_ACTIONS: dict[int, str] | None = None


def _slider_actions() -> dict[int, str]:
    global _ACTIONS
    if _ACTIONS is None:
        a = QAbstractSlider.SliderAction
        _ACTIONS = {
            a.SliderSingleStepAdd.value: "+1", a.SliderSingleStepSub.value: "-1",
            a.SliderPageStepAdd.value: "+page", a.SliderPageStepSub.value: "-page",
            a.SliderToMinimum.value: "min", a.SliderToMaximum.value: "max",
        }
    return _ACTIONS


# ---------------------------------------------------------------------------------
# the grid
# ---------------------------------------------------------------------------------
class VirtualGrid(QAbstractScrollArea):
    """Virtual list / table / tree. See the module docstring for the coordinate rules."""

    current_changed = Signal(object, int)             # row, column index (-1, -1 when none)
    activated = Signal(object, int)                   # double click / Enter
    expander_clicked = Signal(object)                 # click on the expander of a tree row
    context_menu_requested = Signal(object, int, QPoint)   # row (-1: empty area), column, global pos
    header_clicked = Signal(int, object)              # column index, Qt.KeyboardModifiers
    header_context_menu = Signal(int, QPoint)         # column index (-1: empty header area), global pos
    selection_changed = Signal()
    columns_changed = Signal()                        # user resized / reordered / hid columns
    viewport_changed = Signal()                       # visible rows or horizontal offset changed
    font_changed = Signal()                           # zoom / set_font
    header_geometry_changed = Signal()                # sections moved / resized, gutter, header placed again

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source: Any = None
        self._rows = 0
        self._cols: list[Column] = []
        self._xs: list[int] = [0]          # content x of each column's left edge (hidden: zero width)
        self._fc = -1                      # index of the frozen column or -1
        self._tree_col: int | None = None
        self._frozen = 0
        self._mode = "rows"
        self._row_labels = False
        self._alternating = True
        self._grid_lines = False
        self._header_visible = True
        self._extra: QWidget | None = None   # widget of the strip under the header (set_header_extra)
        self._extra_h = 0
        self._extra_on = False
        self._placeholder = t("gui.grid.empty")
        self._top = 0
        self._hoff = 0
        self._cur_row = -1
        self._cur_col = -1
        self._anchor: tuple[int, int] = (-1, -1)
        self._ranges: list[tuple[int, int, int, int]] = []   # rows mode: (r0, r1, 0, -1) = all columns
        self._base_ranges: list[tuple[int, int, int, int]] = []  # kept while extending with Shift
        self._hover = -1
        self._key_handler: Callable[[QKeyEvent], bool] | None = None
        self._pending_top: int | None = None
        self._syncing = False
        self._last_shown: tuple[int, int] | None = None
        self._last_window: tuple[int, int, int] | None = None
        self._gutter_w = 0
        self._wheel_rows = 0.0
        self._wheel_zoom = 0
        self._dragging = False
        self._drag_pos = QPoint()
        self._colors: _Colors | None = None
        self._pix: dict[tuple, QPixmap] = {}
        self._cell_error_logged = False
        self._zoom = 0.0
        self._pfont = 0                    # index into self._fonts currently set on the painter

        self._auto = QTimer(self)
        self._auto.setInterval(40)
        self._auto.timeout.connect(self._auto_scroll_tick)

        vp = self.viewport()
        vp.setMouseTracking(True)
        vp.setAutoFillBackground(False)
        vp.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._header = GridHeader(self)

        vbar = self.verticalScrollBar()
        vbar.valueChanged.connect(self._on_vbar_value)
        vbar.actionTriggered.connect(self._on_vbar_action)
        self.horizontalScrollBar().valueChanged.connect(self._on_hbar_value)
        theme.notifier().theme_changed.connect(self._on_theme_changed)

        font = theme.mono_font()
        self._base_font = QFont(font)
        self._base_size = self._point_size(font)
        self._apply_font(font, emit=False)
        self._layout()

    # ------------------------------------------------------------------ source / rows
    def set_source(self, source: Any) -> None:
        """Use ``source`` (a :class:`GridSource` or None) and :meth:`reset`."""
        self._source = source
        self.reset()

    def source(self) -> Any:
        return self._source

    def _read_count(self) -> int:
        if self._source is None:
            return 0
        try:
            return max(0, int(self._source.row_count()))
        except Exception:  # noqa: BLE001 - a broken source must not break the GUI
            log.exception("grid source row_count() failed")
            return 0

    def reset(self) -> None:
        """New rows: re-read the count; top row, current row and selection are cleared."""
        self._rows = self._read_count()
        self._top = 0
        self._hover = -1
        self._anchor = (-1, -1)
        self._base_ranges = []
        self._last_shown = None
        self._set_ranges([])
        self._set_current_raw(-1, -1)
        self._sync_vbar()
        self.viewport().update()
        self._refresh_window()

    def rows_changed(self, keep_current: bool = True) -> None:
        """The row count changed (live growth / shrink): keep the top row and, when still
        valid and ``keep_current``, the current row; the selection is clamped."""
        rows = self._read_count()
        self._rows = rows
        if not keep_current or self._cur_row >= rows:
            self._set_current_raw(-1, -1)
        if not keep_current:
            self._set_ranges([])
        else:
            clamped = [(r0, min(r1, rows - 1), c0, c1) for r0, r1, c0, c1 in self._ranges if r0 < rows]
            self._set_ranges(clamped)
            self._base_ranges = [(r0, min(r1, rows - 1), c0, c1)
                                 for r0, r1, c0, c1 in self._base_ranges if r0 < rows]
        if self._anchor[0] >= rows:
            self._anchor = (-1, -1)
        if self._hover >= rows:
            self._hover = -1
        self._sync_vbar()
        self.viewport().update()
        self._refresh_window()

    def refresh(self, request: bool = False) -> None:
        """Repaint (new data arrived). ``request=True`` also re-sends ``rows_shown``."""
        if request:
            self._last_shown = None
        self.viewport().update()
        self._header.update()
        self._refresh_window()

    def row_count(self) -> int:
        """The row count the grid currently uses (last read from the source)."""
        return self._rows

    # ------------------------------------------------------------------ columns
    def set_columns(self, cols: list[Column]) -> None:
        """Replace the columns (display order). Keeps rows, current row and row selection."""
        self._cols = list(cols)
        for col in self._cols:
            col.width = max(MIN_COLUMN_WIDTH, int(col.width))
        n = len(self._cols)
        if self._tree_col is not None and self._tree_col >= n:
            self._tree_col = None
        self._recompute_columns()
        if self._mode == "cells":
            kept = [(r0, r1, c0, min(c1, n - 1)) for r0, r1, c0, c1 in self._ranges if c0 < n]
            self._set_ranges(kept)
            self._base_ranges = []
        col = self._cur_col
        if self._cur_row >= 0 and not self._col_ok(col):
            col = self._default_col()
        self._set_current_raw(self._cur_row, col if self._cur_row >= 0 else -1)
        self._columns_geometry_changed()

    def columns(self) -> list[Column]:
        """The columns in display order (a new list; the Column objects are shared)."""
        return list(self._cols)

    def refresh_columns(self) -> None:
        """Re-read the Column objects after the owner mutated them (titles, sort indicators,
        widths, visibility). Emits nothing."""
        for col in self._cols:
            col.width = max(MIN_COLUMN_WIDTH, int(col.width))
        self._recompute_columns()
        if self._cur_row >= 0 and not self._col_ok(self._cur_col):
            self._set_current_raw(self._cur_row, self._default_col())
        self._columns_geometry_changed()

    def set_column_width(self, index: int, width: int) -> None:
        """Set a column's width (min :data:`MIN_COLUMN_WIDTH`); emits ``columns_changed``."""
        if self._set_width(index, width):
            self.columns_changed.emit()

    def set_column_visible(self, index: int, visible: bool) -> None:
        """Show / hide a column; emits ``columns_changed``."""
        if not 0 <= index < len(self._cols) or self._cols[index].visible == bool(visible):
            return
        self._cols[index].visible = bool(visible)
        self._recompute_columns()
        if self._cur_row >= 0 and not self._col_ok(self._cur_col):
            self._set_current_raw(self._cur_row, self._nearest_visible(self._cur_col))
        self._columns_geometry_changed()
        self.columns_changed.emit()

    def move_column(self, src: int, dst: int) -> None:
        """Move column ``src`` so that it ends at index ``dst``; emits ``columns_changed``."""
        n = len(self._cols)
        if not (0 <= src < n and 0 <= dst < n) or src == dst:
            return
        col = self._cols.pop(src)
        self._cols.insert(dst, col)

        def remap(i: int) -> int:
            if i == src:
                return dst
            if src < i <= dst:
                return i - 1
            if dst <= i < src:
                return i + 1
            return i

        if self._tree_col is not None:
            self._tree_col = remap(self._tree_col)
        if self._anchor[1] >= 0:
            self._anchor = (self._anchor[0], remap(self._anchor[1]))
        if self._mode == "cells":
            self._set_ranges([])
            self._base_ranges = []
        self._recompute_columns()
        if self._cur_col >= 0:
            self._set_current_raw(self._cur_row, remap(self._cur_col))
        self._columns_geometry_changed()
        self.columns_changed.emit()

    def set_sort_indicators(self, keys: list[tuple[int, int]]) -> None:
        """Show sort arrows: ``keys`` = ``[(column index, order 1|-1), ...]`` in priority order
        (an empty list clears them). Emits nothing (sorting is the owner's job)."""
        for col in self._cols:
            col.sort_order = 0
            col.sort_priority = 0
        for prio, (index, order) in enumerate(keys, start=1):
            if 0 <= index < len(self._cols) and order:
                self._cols[index].sort_order = 1 if order > 0 else -1
                self._cols[index].sort_priority = prio
        self._header.update()

    def fit_column(self, index: int) -> None:
        """Width that fits the title and the widest visible cell; emits ``columns_changed``."""
        if not 0 <= index < len(self._cols) or not self._cols[index].visible:
            return
        col = self._cols[index]
        best = self._header.title_width(index)
        if self._source is not None and self._rows > 0:
            first, last = self.visible_rows()
            for row in range(first, last + 1):
                cell = self._fetch(row, col)
                if cell is None:
                    continue
                tx, _ = self._text_area(cell, index, 0, 0)
                fm = self._metrics(cell)
                best = max(best, tx + fm.horizontalAdvance(cell.text[:_FIT_CHARS]) + self._pad_x + 1)
        self.set_column_width(index, _clamp(best, MIN_COLUMN_WIDTH, MAX_FIT_WIDTH))

    def _set_width(self, index: int, width: int) -> bool:
        if not 0 <= index < len(self._cols):
            return False
        width = max(MIN_COLUMN_WIDTH, int(width))
        if self._cols[index].width == width:
            return False
        self._cols[index].width = width
        self._recompute_columns()
        self._columns_geometry_changed()
        return True

    def _recompute_columns(self) -> None:
        xs = [0]
        acc = 0
        for col in self._cols:
            if col.visible:
                acc += col.width
            xs.append(acc)
        self._xs = xs
        self._fc = -1
        if self._frozen:
            for i, col in enumerate(self._cols):
                if col.visible:
                    self._fc = i
                    break

    def _columns_geometry_changed(self) -> None:
        self._sync_hbar()
        self._header.update()
        self.viewport().update()
        self._refresh_window()
        self.header_geometry_changed.emit()

    def _col_ok(self, col: int) -> bool:
        return 0 <= col < len(self._cols) and self._cols[col].visible

    def _visible_indices(self) -> list[int]:
        return [i for i, col in enumerate(self._cols) if col.visible]

    def _default_col(self) -> int:
        if self._tree_col is not None and self._col_ok(self._tree_col):
            return self._tree_col
        vis = self._visible_indices()
        return vis[0] if vis else -1

    def _nearest_visible(self, col: int) -> int:
        vis = self._visible_indices()
        if not vis:
            return -1
        after = [i for i in vis if i >= col]
        return after[0] if after else vis[-1]

    # ------------------------------------------------------------------ options
    def set_tree_column(self, index: int | None) -> None:
        """Column painted with indent guides, expander and type badge (None: no tree)."""
        self._tree_col = index if index is not None and 0 <= index < len(self._cols) else None
        self.viewport().update()

    def tree_column(self) -> int | None:
        return self._tree_col

    def set_frozen_columns(self, n: int) -> None:
        """0 or 1: the first visible column stays in place while scrolling horizontally."""
        self._frozen = 1 if n else 0
        self._recompute_columns()
        self._columns_geometry_changed()

    def frozen_columns(self) -> int:
        return self._frozen

    def set_selection_mode(self, mode: str) -> None:
        """``single`` (current row only) | ``rows`` (extended rows) | ``cells`` (rectangles)."""
        if mode not in SELECTION_MODES:
            raise ValueError(f"unknown selection mode: {mode!r}")
        if mode == self._mode:
            return
        self._mode = mode
        self._base_ranges = []
        self._anchor = (self._cur_row, self._cur_col)
        if mode == "single" or self._cur_row < 0:
            self._set_ranges([])
        else:
            self._set_ranges([self._single(self._cur_row, self._cur_col)])
        self.viewport().update()

    def selection_mode(self) -> str:
        return self._mode

    def set_row_labels(self, on: bool) -> None:
        """Show a gutter with ``source.row_label(row)`` on the left."""
        self._row_labels = bool(on)
        self._update_gutter()
        self.viewport().update()

    def set_alternating(self, on: bool) -> None:
        self._alternating = bool(on)
        self.viewport().update()

    def set_grid_lines(self, on: bool) -> None:
        """Thin separators between cells (tables)."""
        self._grid_lines = bool(on)
        self.viewport().update()

    def set_header_visible(self, on: bool) -> None:
        self._header_visible = bool(on)
        self._header.setVisible(self._header_visible)
        self._layout()

    def header(self) -> GridHeader:
        return self._header

    def set_header_extra(self, widget: QWidget | None, height: int | None = None) -> None:
        """Reserve a strip of ``height`` pixels (default: the widget's size hint) right under
        the header for ``widget`` (e.g. a filter row). The grid re-parents the widget and
        keeps it as wide as the header; its x axis is the header's (use
        ``header().section_rect(i)``); re-arrange its children on
        :attr:`header_geometry_changed`. A new widget starts visible; calling this again
        with the same widget only changes the height. ``None`` removes the strip (the old
        widget is hidden, not deleted)."""
        old = self._extra
        if old is not None and old is not widget:
            old.hide()
        if widget is None:
            self._extra, self._extra_h, self._extra_on = None, 0, False
        else:
            if widget is not old:
                self._extra_on = True
            if widget.parentWidget() is not self:
                widget.setParent(self)
            self._extra = widget
            self._extra_h = max(0, int(height if height is not None else widget.sizeHint().height()))
            widget.setVisible(self._extra_on and self._extra_h > 0)
        self._layout()

    def set_header_extra_visible(self, on: bool) -> None:
        """Show / hide the strip of :meth:`set_header_extra` (hidden: it takes no room)."""
        self._extra_on = bool(on)
        if self._extra is not None:
            self._extra.setVisible(self._extra_on and self._extra_h > 0)
        self._layout()

    def header_extra(self) -> QWidget | None:
        return self._extra

    def header_extra_visible(self) -> bool:
        return self._extra is not None and self._extra_on

    def header_extra_height(self) -> int:
        """Pixels the strip under the header takes now (0: none or hidden)."""
        return self._extra_h if self._extra is not None and self._extra_on else 0

    def set_placeholder_text(self, text: str) -> None:
        """Text shown centered when ``row_count() == 0``."""
        self._placeholder = text or ""
        if self._rows == 0:
            self.viewport().update()

    def set_key_handler(self, fn: Callable[[QKeyEvent], bool] | None) -> None:
        """``fn(QKeyEvent) -> bool`` runs before the grid's own keys; True consumes the key."""
        self._key_handler = fn

    # ------------------------------------------------------------------ fonts / zoom
    @staticmethod
    def _point_size(font: QFont) -> float:
        size = font.pointSizeF()
        if size <= 0:
            size = QFontInfo(font).pointSizeF()
        return size if size > 0 else 10.0

    def set_font(self, font: QFont, *, keep_zoom: bool = False) -> None:
        """Data font (the header keeps the widget font); resets the zoom unless
        ``keep_zoom`` (then the zoom steps apply to the new font: a font chosen in the
        settings while the view is zoomed)."""
        self._base_font = QFont(font)
        self._base_size = self._point_size(font)
        applied = QFont(font)
        if keep_zoom and self._zoom:
            size = _clamp_f(self._base_size + self._zoom, _MIN_POINT_SIZE, _MAX_POINT_SIZE)
            self._zoom = size - self._base_size
            applied.setPointSizeF(size)
        else:
            self._zoom = 0.0
        self._apply_font(applied)

    def grid_font(self) -> QFont:
        return QFont(self._font)

    def base_font(self) -> QFont:
        """The font given to :meth:`set_font` (without the zoom)."""
        return QFont(self._base_font)

    def zoom(self, steps: int) -> None:
        """Change the data font size by ``steps`` points (0 restores the original size)."""
        if steps == 0:
            self._zoom = 0.0
        else:
            size = _clamp_f(self._base_size + self._zoom + steps, _MIN_POINT_SIZE, _MAX_POINT_SIZE)
            self._zoom = size - self._base_size
        font = QFont(self._base_font)
        font.setPointSizeF(self._base_size + self._zoom)
        self._apply_font(font)

    def zoom_level(self) -> float:
        """Points added by :meth:`zoom` to the font given to :meth:`set_font`."""
        return self._zoom

    def row_height(self) -> int:
        return self._row_h

    def indent(self) -> int:
        return self._indent

    def _apply_font(self, font: QFont, *, emit: bool = True) -> None:
        self._font = QFont(font)
        bold = QFont(font)
        bold.setBold(True)
        italic = QFont(font)
        italic.setItalic(True)
        bold_italic = QFont(bold)
        bold_italic.setItalic(True)
        self._fonts = (self._font, bold, italic, bold_italic)
        self._fms = tuple(QFontMetrics(f) for f in self._fonts)
        fm = self._fms[0]
        self._fm = fm
        h = fm.height()
        self._pad_y = max(3, h // 4)
        self._row_h = h + 2 * self._pad_y
        self._text_dy = self._pad_y + fm.ascent()
        self._char_w = max(1, fm.averageCharWidth())
        self._pad_x = max(4, round(self._char_w * 0.75))
        self._gap = max(3, h // 4)
        self._indent = max(12, round(h * 1.05))
        small = QFont(font)
        small.setPointSizeF(max(_MIN_POINT_SIZE, self._point_size(font) * 0.78))
        small.setWeight(QFont.Weight.Medium)
        self._badge_font = small
        bfm = QFontMetrics(small)
        self._badge_w = max(bfm.horizontalAdvance(g) for g in theme.BADGES.values()) + 8
        self._badge_h = min(self._row_h - 2, bfm.height() + 3)
        self._pix.clear()
        self._sync_vbar()
        self._update_gutter()
        self._sync_hbar()
        self.viewport().update()
        self._header.update()
        self._refresh_window()
        if emit:
            self.font_changed.emit()

    def _metrics(self, cell: Cell) -> QFontMetrics:
        return self._fms[(1 if cell.bold else 0) + (2 if cell.italic else 0)]

    # ------------------------------------------------------------------ geometry
    def _page_rows(self) -> int:
        """Rows that fit completely into the viewport (at least 1)."""
        return max(1, self.viewport().height() // self._row_h)

    def _window_rows(self) -> int:
        """Rows touched by the viewport (the last one may be partial)."""
        return max(1, -(-self.viewport().height() // self._row_h))

    def _max_top(self) -> int:
        return max(0, self._rows - self._page_rows())

    def visible_rows(self) -> tuple[int, int]:
        """(first, last) rows touched by the viewport; ``(0, -1)`` when there are no rows."""
        if self._rows <= 0:
            return (0, -1)
        return (self._top, min(self._rows - 1, self._top + self._window_rows() - 1))

    def top_row(self) -> int:
        return self._top

    def horizontal_offset(self) -> int:
        return self._hoff

    def gutter_width(self) -> int:
        return self._gutter_w

    def _scroll_left(self) -> int:
        """Viewport x where the scrollable columns become visible (right of gutter/frozen)."""
        left = self._gutter_w
        if self._fc >= 0:
            left += self._cols[self._fc].width
        return left

    def _section_x(self, index: int) -> int:
        if index == self._fc:
            return self._gutter_w
        return self._gutter_w + self._xs[index] - self._hoff

    def _content_right(self) -> int:
        return self._gutter_w + self._xs[-1] - self._hoff

    def _max_hoff(self) -> int:
        return max(0, self._xs[-1] - (self.viewport().width() - self._gutter_w))

    def row_at(self, y: int) -> int:
        """Row at viewport ``y`` or -1."""
        if y < 0 or self._rows <= 0:
            return -1
        row = self._top + y // self._row_h
        return row if row < self._rows else -1

    def column_at(self, x: int) -> int:
        """Column index at viewport ``x`` or -1 (gutter, right of the last column)."""
        gw = self._gutter_w
        if x < gw or not self._cols:
            return -1
        fc = self._fc
        if fc >= 0 and x < gw + self._cols[fc].width:
            return fc
        cx = x - gw + self._hoff
        xs = self._xs
        if cx < 0 or cx >= xs[-1]:
            return -1
        i = bisect_right(xs, cx) - 1
        if i >= len(self._cols):
            return -1
        return i if self._cols[i].visible else -1

    def cell_rect(self, row: int, col: int) -> QRect:
        """Viewport rect of a cell (null rect for hidden columns / invalid rows)."""
        if not (0 <= row < self._rows) or not self._col_ok(col):
            return QRect()
        y = _clamp((row - self._top) * self._row_h, -_Y_LIMIT, _Y_LIMIT)
        return QRect(self._section_x(col), y, self._cols[col].width, self._row_h)

    def row_rect(self, row: int) -> QRect:
        """Viewport rect of a whole row (gutter excluded)."""
        if not 0 <= row < self._rows:
            return QRect()
        y = _clamp((row - self._top) * self._row_h, -_Y_LIMIT, _Y_LIMIT)
        return QRect(self._gutter_w, y, max(0, self.viewport().width() - self._gutter_w), self._row_h)

    def _text_area(self, cell: Cell, index: int, x: int, width: int) -> tuple[int, int]:
        """(text x, right edge) of a cell drawn at ``x`` with ``width``; shared by painting,
        hit testing, tooltips and "fit column"."""
        left = x + self._pad_x
        tx = left
        if index == self._tree_col:
            tx += cell.depth * self._indent + self._indent
        if cell.badge:
            tx += self._badge_w + self._gap
        return tx, x + width - self._pad_x

    def expander_rect(self, row: int) -> QRect:
        """Viewport rect of the expander of ``row`` (null when the row has none)."""
        index = self._tree_col
        if index is None or not self._col_ok(index) or not 0 <= row < self._rows:
            return QRect()
        cell = self._fetch(row, self._cols[index])
        if cell is None or not cell.expander:
            return QRect()
        rect = self.cell_rect(row, index)
        ex = rect.x() + self._pad_x + cell.depth * self._indent
        if ex + self._indent > rect.x() + rect.width():   # does not fit: not painted either
            return QRect()
        return QRect(ex, rect.y(), self._indent, self._row_h)

    # ------------------------------------------------------------------ scrolling
    def _top_to_value(self, top: int) -> int:
        m = self._max_top()
        if m <= _INT_MAX:
            return top
        return (top * _SCALE + m // 2) // m

    def _value_to_top(self, value: int) -> int:
        m = self._max_top()
        if m <= _INT_MAX:
            return value
        if value >= _SCALE:
            return m
        return (value * m + _SCALE // 2) // _SCALE

    def _sync_vbar(self) -> None:
        bar = self.verticalScrollBar()
        m = self._max_top()
        self._top = _clamp(self._top, 0, m)
        page = self._page_rows()
        self._syncing = True
        try:
            if m <= _INT_MAX:
                bar.setRange(0, m)
                bar.setPageStep(page)
            else:
                bar.setRange(0, _SCALE)
                bar.setPageStep(max(1, page * _SCALE // m))
            bar.setSingleStep(1)
            bar.setValue(self._top_to_value(self._top))
        finally:
            self._syncing = False

    def _sync_hbar(self) -> None:
        bar = self.horizontalScrollBar()
        m = self._max_hoff()
        self._hoff = _clamp(self._hoff, 0, m)
        self._syncing = True
        try:
            bar.setRange(0, m)
            bar.setPageStep(max(1, self.viewport().width() - self._scroll_left()))
            bar.setSingleStep(max(8, self._char_w * 3))
            bar.setValue(self._hoff)
        finally:
            self._syncing = False

    def _on_vbar_action(self, action: int) -> None:
        """Scaled range: make arrow / page clicks move exact rows."""
        if self._max_top() <= _INT_MAX:
            return
        kind = _slider_actions().get(int(action))
        if kind is None:
            return
        page = self._page_rows()
        target = {"+1": self._top + 1, "-1": self._top - 1, "+page": self._top + page,
                  "-page": self._top - page, "min": 0, "max": self._max_top()}[kind]
        target = _clamp(target, 0, self._max_top())
        bar = self.verticalScrollBar()
        value = self._top_to_value(target)
        if value == bar.value():  # no valueChanged will follow
            bar.setSliderPosition(value)
            self._pending_top = None
            self._set_top(target, sync_bar=False)
        else:
            self._pending_top = target
            bar.setSliderPosition(value)

    def _on_vbar_value(self, value: int) -> None:
        if self._syncing:
            return
        pending, self._pending_top = self._pending_top, None
        if pending is not None and self._top_to_value(pending) == value:
            top = pending
        else:
            top = self._value_to_top(value)
        self._set_top(top, sync_bar=False)

    def _on_hbar_value(self, value: int) -> None:
        if self._syncing:
            return
        self._set_hoff(value, sync_bar=False)

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802 - Qt override
        """Scrolling is driven by our own scrollbar slots (rows, not pixels)."""

    def _set_top(self, top: int, *, sync_bar: bool = True) -> bool:
        top = _clamp(top, 0, self._max_top())
        if top == self._top:
            if sync_bar:
                self._sync_vbar()
            return False
        self._top = top
        if sync_bar:
            self._sync_vbar()
        if self._hover >= 0:  # the row under the mouse changed
            vp = self.viewport()
            self._hover = self.row_at(vp.mapFromGlobal(QCursor.pos()).y()) if vp.underMouse() else -1
        self.viewport().update()
        self._refresh_window()
        return True

    def _set_hoff(self, x: int, *, sync_bar: bool = True) -> bool:
        x = _clamp(int(x), 0, self._max_hoff())
        if x == self._hoff:
            return False
        self._hoff = x
        if sync_bar:
            self._sync_hbar()
        self.viewport().update()
        self._header.update()
        self._refresh_window()
        self.header_geometry_changed.emit()
        return True

    def set_horizontal_offset(self, x: int) -> None:
        self._set_hoff(x)

    def scroll_to(self, row: int, where: str = "ensure") -> None:
        """Scroll so that ``row`` is visible: ``ensure`` (minimal move) | ``center`` | ``top``
        | ``bottom``."""
        if self._rows <= 0 or where == "none":
            return
        row = _clamp(int(row), 0, self._rows - 1)
        page = self._page_rows()
        if where == "top":
            top = row
        elif where == "center":
            top = row - (page - 1) // 2
        elif where == "bottom":
            top = row - page + 1
        else:
            if row < self._top:
                top = row
            elif row >= self._top + page:
                top = row - page + 1
            else:
                return
        self._set_top(top)

    def scroll_to_column(self, index: int) -> None:
        """Scroll horizontally so that column ``index`` is visible (no-op for the frozen one)."""
        if not self._col_ok(index) or index == self._fc:
            return
        x0 = self._xs[index]
        x1 = x0 + self._cols[index].width
        frozen_w = self._cols[self._fc].width if self._fc >= 0 else 0
        area = self.viewport().width() - self._gutter_w
        hoff = self._hoff
        if x0 - hoff < frozen_w:
            hoff = x0 - frozen_w
        elif x1 - hoff > area:
            hoff = min(x1 - area, x0 - frozen_w)
        self._set_hoff(hoff)

    def _refresh_window(self) -> None:
        """Send ``rows_shown`` / ``viewport_changed`` when the visible window changed."""
        self._update_gutter()
        first, last = self.visible_rows()
        if self._source is not None and self._rows > 0 and self.isVisible():
            prefetch = min(self._rows - 1, last + self._page_rows())
            key = (first, prefetch)
            if key != self._last_shown:
                self._last_shown = key
                fn = getattr(self._source, "rows_shown", None)
                if fn is not None:
                    try:
                        fn(first, prefetch)
                    except Exception:  # noqa: BLE001
                        log.exception("grid source rows_shown() failed")
        window = (first, last, self._hoff)
        if window != self._last_window:
            self._last_window = window
            self.viewport_changed.emit()

    def _update_gutter(self) -> None:
        width = 0
        fn = getattr(self._source, "row_label", None) if self._row_labels else None
        if self._row_labels:
            fm = self._fm
            width = fm.horizontalAdvance("#0")
            if fn is not None and self._rows > 0:
                first, last = self.visible_rows()
                for row in range(first, last + 1):
                    try:
                        label = fn(row)
                    except Exception:  # noqa: BLE001
                        label = None
                    if label:
                        width = max(width, fm.horizontalAdvance(label))
            width += 2 * self._pad_x
        if width != self._gutter_w:
            self._gutter_w = width
            self._sync_hbar()
            self._header.update()
            self.viewport().update()
            self.header_geometry_changed.emit()

    # ------------------------------------------------------------------ layout / events
    def _layout(self) -> None:
        h = self._header.sizeHint().height() if self._header_visible else 0
        self.setViewportMargins(0, h + self.header_extra_height(), 0, 0)
        self._place_header()

    def _place_header(self) -> None:
        vg = self.viewport().geometry()
        eh = self.header_extra_height()
        if self._header_visible:
            h = self._header.sizeHint().height()
            self._header.setGeometry(vg.x(), vg.y() - eh - h, vg.width(), h)
        if self._extra is not None and eh:
            self._extra.setGeometry(vg.x(), vg.y() - eh, vg.width(), eh)
        self.header_geometry_changed.emit()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_header()

    def _on_viewport_resized(self) -> None:
        self._place_header()
        self._sync_vbar()
        self._sync_hbar()
        self.viewport().update()
        self._refresh_window()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._refresh_window()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._last_shown = None      # hidden views do no I/O; ask again when shown

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        kind = event.type()
        if kind in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            self._on_theme_changed()
        elif kind == QEvent.Type.FontChange:
            self._layout()
            self._header.update()

    def _on_theme_changed(self) -> None:
        self._colors = None
        self._pix.clear()
        self.viewport().update()
        self._header.update()

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.viewport().update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.viewport().update()

    def viewportEvent(self, event: QEvent) -> bool:  # noqa: N802
        kind = event.type()
        if kind == QEvent.Type.Resize:
            result = super().viewportEvent(event)
            self._on_viewport_resized()
            return result
        if kind == QEvent.Type.ToolTip:
            pos = event.pos()
            text = self.tooltip_at(pos)
            if text:
                row, col = self.row_at(pos.y()), self.column_at(pos.x())
                rect = self.cell_rect(row, col) if col >= 0 else self.row_rect(row)
                QToolTip.showText(event.globalPos(), text, self.viewport(), rect)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        if kind == QEvent.Type.Leave:
            self._set_hover(-1)
        return super().viewportEvent(event)

    # ------------------------------------------------------------------ current / selection
    def current(self) -> tuple[int, int]:
        """(row, column index); (-1, -1) when there is no current cell."""
        return (self._cur_row, self._cur_col)

    def _set_current_raw(self, row: int, col: int) -> None:
        if row < 0:
            row, col = -1, -1
        if (row, col) != (self._cur_row, self._cur_col):
            self._cur_row, self._cur_col = row, col
            self.viewport().update()
            self.current_changed.emit(row, col)

    def set_current(self, row: int, col: int | None = None, *, select: bool = True,
                    scroll: str = "ensure") -> None:
        """Make (row, col) current (``col`` None keeps the current column). ``select``
        replaces the selection by it; ``scroll``: ensure | center | top | bottom | none.
        ``row < 0`` clears the current cell."""
        if row is None or row < 0 or self._rows <= 0:
            self._set_current_raw(-1, -1)
            return
        row = _clamp(int(row), 0, self._rows - 1)
        if col is None or not self._col_ok(col):
            col = self._cur_col if self._col_ok(self._cur_col) else self._default_col()
        if select and self._mode != "single":
            self._anchor = (row, col)
            self._base_ranges = []
            self._set_ranges([self._single(row, col)])
        self._set_current_raw(row, col)
        if scroll != "none":
            self.scroll_to(row, scroll)
            if self._mode == "cells":
                self.scroll_to_column(col)

    def _single(self, row: int, col: int) -> tuple[int, int, int, int]:
        if self._mode == "cells":
            return (row, row, col, col)
        return (row, row, 0, -1)

    def _span(self, a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int, int, int]:
        r0, r1 = (a[0], b[0]) if a[0] <= b[0] else (b[0], a[0])
        if self._mode != "cells":
            return (r0, r1, 0, -1)
        c0, c1 = (a[1], b[1]) if a[1] <= b[1] else (b[1], a[1])
        return (r0, r1, max(0, c0), max(0, c1))

    def _set_ranges(self, ranges: list[tuple[int, int, int, int]]) -> None:
        if ranges != self._ranges:
            self._ranges = ranges
            self.viewport().update()
            self.selection_changed.emit()

    def selected_ranges(self) -> list[tuple[int, int, int, int]]:
        """Inclusive ``(row0, row1, col0, col1)`` rectangles. ``rows``/``single`` modes give
        full-width ranges (col0 = 0, col1 = len(columns) - 1). Ranges may overlap."""
        last_col = len(self._cols) - 1
        if self._mode == "single":
            return [(self._cur_row, self._cur_row, 0, last_col)] if self._cur_row >= 0 else []
        if self._mode == "rows":
            return [(r0, r1, 0, last_col) for r0, r1, _c0, _c1 in self._ranges]
        return list(self._ranges)

    def selected_row_count(self) -> int:
        """Number of distinct selected rows (O(k log k) in the number of ranges)."""
        spans = sorted((r0, r1) for r0, r1, *_ in self.selected_ranges())
        total = 0
        end = -1
        for r0, r1 in spans:
            if r1 <= end:
                continue
            total += r1 - max(r0, end + 1) + 1
            end = r1
        return total

    def is_selected(self, row: int, col: int | None = None) -> bool:
        """Whether ``row`` (rows modes) or the cell (row, col) (cells mode) is selected."""
        if self._mode == "single":
            return row == self._cur_row and row >= 0
        if self._mode == "cells" and col is not None:
            return self._cell_selected(row, col)
        return self._row_selected(row)

    def _row_selected(self, row: int) -> bool:
        if self._mode == "single":
            return row == self._cur_row
        for r0, r1, _c0, _c1 in self._ranges:
            if r0 <= row <= r1:
                return True
        return False

    def _cell_selected(self, row: int, col: int) -> bool:
        for r0, r1, c0, c1 in self._ranges:
            if r0 <= row <= r1 and (c1 < 0 or c0 <= col <= c1):
                return True
        return False

    def clear_selection(self) -> None:
        self._base_ranges = []
        self._set_ranges([])

    def select_all(self) -> None:
        """Select every row (O(1)); no-op in ``single`` mode."""
        if self._mode == "single" or self._rows <= 0:
            return
        self._base_ranges = []
        if self._mode == "rows":
            self._set_ranges([(0, self._rows - 1, 0, -1)])
        elif self._cols:
            self._set_ranges([(0, self._rows - 1, 0, len(self._cols) - 1)])

    def _toggle(self, row: int, col: int) -> bool:
        """Toggle one row/cell; True when it is selected afterwards."""
        if self._mode == "cells":
            hit = [r for r in self._ranges if r[0] <= row <= r[1] and r[2] <= col <= r[3]]
        else:
            hit = [r for r in self._ranges if r[0] <= row <= r[1]]
        if not hit:
            self._set_ranges(self._ranges + [self._single(row, col)])
            return True
        out: list[tuple[int, int, int, int]] = []
        for rng in self._ranges:
            if rng not in hit:
                out.append(rng)
                continue
            r0, r1, c0, c1 = rng
            if r0 < row:
                out.append((r0, row - 1, c0, c1))
            if row < r1:
                out.append((row + 1, r1, c0, c1))
            if self._mode == "cells":
                if c0 < col:
                    out.append((row, row, c0, col - 1))
                if col < c1:
                    out.append((row, row, col + 1, c1))
        self._set_ranges(out)
        return False

    def _extend_to(self, row: int, col: int) -> None:
        anchor = self._anchor
        if anchor[0] < 0 or anchor[0] >= self._rows:
            anchor = (self._cur_row, self._cur_col) if self._cur_row >= 0 else (row, col)
            self._anchor = anchor
        if anchor[1] < 0:
            anchor = (anchor[0], col)
        self._set_ranges(self._base_ranges + [self._span(anchor, (row, col))])

    # ------------------------------------------------------------------ keyboard
    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self._key_handler is not None:
            try:
                if self._key_handler(event):
                    event.accept()
                    return
            except Exception:  # noqa: BLE001
                log.exception("grid key handler failed")
        if self._handle_key(event):
            event.accept()
        else:
            event.ignore()

    def _handle_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        mods = event.modifiers()
        K = Qt.Key
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        if event.matches(QKeySequence.StandardKey.ZoomIn) or (ctrl and key in (K.Key_Plus, K.Key_Equal)):
            self.zoom(1)
            return True
        if event.matches(QKeySequence.StandardKey.ZoomOut) or (ctrl and key == K.Key_Minus):
            self.zoom(-1)
            return True
        if ctrl and key == K.Key_0:
            self.zoom(0)
            return True
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all()
            return True
        if key in (K.Key_Return, K.Key_Enter):
            if self._cur_row >= 0:
                self.activated.emit(self._cur_row, self._cur_col)
                return True
            return False
        if key in (K.Key_Left, K.Key_Right) and self._mode != "cells":
            step = self.horizontalScrollBar().singleStep()
            self._set_hoff(self._hoff + (step if key == K.Key_Right else -step))
            return True
        nav = (K.Key_Up, K.Key_Down, K.Key_PageUp, K.Key_PageDown, K.Key_Home, K.Key_End,
               K.Key_Left, K.Key_Right)
        if key == K.Key_Space and ctrl and self._mode != "single" and self._cur_row >= 0:
            self._anchor = (self._cur_row, self._cur_col)
            self._base_ranges = list(self._ranges)
            self._toggle(self._cur_row, self._cur_col)
            return True
        if key not in nav or self._rows <= 0:
            return False
        has_current = self._cur_row >= 0
        row = self._cur_row if has_current else self._top
        col = self._cur_col if self._col_ok(self._cur_col) else self._default_col()
        page = self._page_rows()
        last = self._rows - 1
        cells = self._mode == "cells"
        if key == K.Key_Up:
            row = row - 1 if has_current else row
        elif key == K.Key_Down:
            row = row + 1 if has_current else row
        elif key == K.Key_PageUp:
            row -= page
        elif key == K.Key_PageDown:
            row += page
        elif key == K.Key_Home:
            if ctrl or not cells:
                row = 0
            else:
                vis = self._visible_indices()
                col = vis[0] if vis else col
        elif key == K.Key_End:
            if ctrl or not cells:
                row = last
            else:
                vis = self._visible_indices()
                col = vis[-1] if vis else col
        elif key in (K.Key_Left, K.Key_Right):
            vis = self._visible_indices()
            if not vis:
                return True
            if col not in vis:
                col = vis[0]
            else:
                pos = vis.index(col) + (1 if key == K.Key_Right else -1)
                col = vis[_clamp(pos, 0, len(vis) - 1)]
        row = _clamp(row, 0, last)
        if shift and self._mode != "single":
            if not has_current:
                self._anchor = (row, col)
            self._set_current_raw(row, col)
            self._extend_to(row, col)
        elif ctrl and self._mode != "single" and key in (K.Key_Up, K.Key_Down, K.Key_PageUp, K.Key_PageDown,
                                                         K.Key_Left, K.Key_Right):
            self._set_current_raw(row, col)        # move without selecting (Ctrl+Space toggles)
        else:
            self.set_current(row, col, select=True, scroll="none")
        self.scroll_to(row, "ensure")
        if cells:
            self.scroll_to_column(col)
        return True

    # ------------------------------------------------------------------ mouse
    def _event_pos(self, event) -> QPoint:
        return event.position().toPoint()

    def _hit_expander(self, row: int, col: int, x: int) -> bool:
        if col < 0 or col != self._tree_col:
            return False
        rect = self.expander_rect(row)
        return rect.isValid() and rect.left() <= x <= rect.right()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        pos = self._event_pos(event)
        row, col = self.row_at(pos.y()), self.column_at(pos.x())
        button = event.button()
        if row < 0:
            event.accept()
            return
        if col < 0:
            col = self._cur_col if self._col_ok(self._cur_col) else self._default_col()
        if button == Qt.MouseButton.LeftButton:
            if self._hit_expander(row, col, pos.x()):
                self.expander_clicked.emit(row)
                event.accept()
                return
            mods = event.modifiers()
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
            if self._mode == "single":
                self.set_current(row, col, scroll="none")
            elif shift:
                self._set_current_raw(row, col)
                self._extend_to(row, col)
                self._dragging = True
            elif ctrl:
                before = list(self._ranges)
                self._anchor = (row, col)
                selected = self._toggle(row, col)
                # Shift extends from here on top of the rest of the selection
                self._base_ranges = before if selected else list(self._ranges)
                self._set_current_raw(row, col)
                self._dragging = selected
            else:
                self.set_current(row, col, scroll="none")
                self._dragging = True
            self._drag_pos = pos
        elif button == Qt.MouseButton.RightButton:
            if not self.is_selected(row, col):
                self.set_current(row, col, scroll="none")
            else:
                self._set_current_raw(row, col)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = self._event_pos(event)
        if self._dragging:
            self._drag_pos = pos
            self._drag_select(pos)
            vp = self.viewport()
            outside = pos.y() < 0 or pos.y() >= vp.height() or (
                self._mode == "cells" and (pos.x() < self._scroll_left() or pos.x() >= vp.width()))
            if outside and not self._auto.isActive():
                self._auto.start()
            elif not outside:
                self._auto.stop()
        else:
            self._set_hover(self.row_at(pos.y()))
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._dragging = False
        self._auto.stop()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pos = self._event_pos(event)
        row, col = self.row_at(pos.y()), self.column_at(pos.x())
        if row < 0 or event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        if self._hit_expander(row, col, pos.x()):
            self.expander_clicked.emit(row)
        else:
            self.activated.emit(row, col if col >= 0 else self._cur_col)
        event.accept()

    def _drag_select(self, pos: QPoint) -> None:
        if self._rows <= 0:
            return
        row = _clamp(self._top + math.floor(pos.y() / self._row_h), 0, self._rows - 1)
        col = self.column_at(_clamp(pos.x(), self._gutter_w, max(self._gutter_w, self.viewport().width() - 1)))
        if col < 0:
            vis = self._visible_indices()
            if not vis:
                return
            col = vis[-1] if pos.x() >= self._scroll_left() else vis[0]
        if (row, col) != (self._cur_row, self._cur_col) or not self._ranges:
            self._set_current_raw(row, col)
            self._extend_to(row, col)

    def _auto_scroll_tick(self) -> None:
        if not self._dragging:
            self._auto.stop()
            return
        pos = self._drag_pos
        vp = self.viewport()
        rh = self._row_h
        if pos.y() < 0:
            self._set_top(self._top - 1 - (-pos.y()) // rh)
        elif pos.y() >= vp.height():
            self._set_top(self._top + 1 + (pos.y() - vp.height()) // rh)
        if self._mode == "cells":
            step = self.horizontalScrollBar().singleStep()
            if pos.x() < self._scroll_left():
                self._set_hoff(self._hoff - step)
            elif pos.x() >= vp.width():
                self._set_hoff(self._hoff + step)
        self._drag_select(pos)

    def _set_hover(self, row: int) -> None:
        if row == self._hover:
            return
        old, self._hover = self._hover, row
        vp = self.viewport()
        for r in (old, row):
            if r >= 0:
                vp.update(0, _clamp((r - self._top) * self._row_h, -_Y_LIMIT, _Y_LIMIT), vp.width(), self._row_h)

    def hover_row(self) -> int:
        return self._hover

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        delta = event.angleDelta()
        dy, dx = delta.y(), delta.x()
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier:
            self._wheel_zoom += dy
            steps = int(self._wheel_zoom / 120)
            if steps:
                self._wheel_zoom -= steps * 120
                self.zoom(steps)
            event.accept()
            return
        if mods & Qt.KeyboardModifier.ShiftModifier and dy and not dx:
            dx, dy = dy, 0
        if dx:
            step = self.horizontalScrollBar().singleStep() * WHEEL_ROWS
            self._set_hoff(self._hoff - round(dx * step / 120))
        if dy:
            self._wheel_rows -= dy * WHEEL_ROWS / 120
            rows = int(self._wheel_rows)
            if rows:
                self._wheel_rows -= rows
                self._set_top(self._top + rows)
        event.accept()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        if event.reason() == QContextMenuEvent.Reason.Keyboard:
            row, col = self._cur_row, self._cur_col
            rect = self.cell_rect(row, col) if col >= 0 else self.row_rect(row)
            local = rect.center() if rect.isValid() else QPoint(self._gutter_w + 8, 8)
            gpos = self.viewport().mapToGlobal(local)
        else:
            pos = event.pos()
            row, col = self.row_at(pos.y()), self.column_at(pos.x())
            if row < 0:
                col = -1
            gpos = event.globalPos()
        self.context_menu_requested.emit(row, col, gpos)
        event.accept()

    # ------------------------------------------------------------------ tooltips
    def tooltip_at(self, pos: QPoint) -> str | None:
        """Tooltip for viewport ``pos``: ``Cell.tooltip``, else the full text when elided."""
        row, col = self.row_at(pos.y()), self.column_at(pos.x())
        if row < 0 or col < 0 or self._source is None:
            return None
        column = self._cols[col]
        cell = self._fetch(row, column)
        if cell is None:
            return None
        if cell.tooltip:
            return _wrap_tip(cell.tooltip)
        if col == self._tree_col and cell.expander == 3 and self._hit_expander(row, col, pos.x()):
            return t("gui.common.loading")
        if not cell.text:
            return None
        rect = self.cell_rect(row, col)
        tx, right = self._text_area(cell, col, rect.x(), rect.width())
        avail = right - tx
        fm = self._metrics(cell)
        limit = max(0, avail) * 3 // self._char_w + 8
        sample = cell.text if len(cell.text) <= limit else cell.text[:limit]
        if len(cell.text) > limit or fm.horizontalAdvance(sample) > avail:
            return _wrap_tip(cell.text)
        return None

    # ------------------------------------------------------------------ painting
    def _fetch(self, row: int, column: Column) -> Cell | None:
        try:
            return self._source.cell(row, column)
        except Exception:  # noqa: BLE001 - one broken cell must not stop painting
            if not self._cell_error_logged:
                self._cell_error_logged = True
                log.exception("grid source cell() failed (row %s, column %s)", row, column.id)
            return None

    def _palette_colors(self) -> _Colors:
        if self._colors is None:
            self._colors = _Colors(self.viewport().palette())
        return self._colors

    def _elide(self, fm: QFontMetrics, text: str, width: int) -> tuple[str, int]:
        """(shown text, number of source characters shown before any ellipsis)."""
        if width <= 0 or not text:
            return "", 0
        limit = width * 3 // self._char_w + 8
        truncated = len(text) > limit
        if truncated:
            text = text[:limit]
        elif fm.horizontalAdvance(text) <= width:
            return text, len(text)
        shown = fm.elidedText(text, Qt.TextElideMode.ElideRight, width)
        if shown == text:  # only possible after truncation: force the ellipsis
            shown = text[:-1] + ELLIPSIS
        kept = len(shown) - 1 if shown.endswith(ELLIPSIS) else len(shown)
        return shown, kept

    def _pixmap(self, key: tuple, w: int, h: int, draw: Callable[[QPainter], None]) -> QPixmap:
        dpr = self.viewport().devicePixelRatioF()
        full = key + (dpr,)
        pm = self._pix.get(full)
        if pm is None:
            pm = QPixmap(max(1, math.ceil(w * dpr)), max(1, math.ceil(h * dpr)))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            q = QPainter(pm)
            q.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            q.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            draw(q)
            q.end()
            self._pix[full] = pm
        return pm

    def _badge_pixmap(self, kind: str) -> QPixmap:
        bw, bh = self._badge_w, self._badge_h

        def draw(q: QPainter) -> None:
            q.setPen(Qt.PenStyle.NoPen)
            q.setBrush(theme.badge_background(kind))
            radius = min(4.0, bh / 3)
            q.drawRoundedRect(QRectF(0.5, 0.5, bw - 1, bh - 1), radius, radius)
            q.setPen(theme.badge_color(kind))
            q.setFont(self._badge_font)
            q.drawText(QRectF(0, 0, bw, bh), int(Qt.AlignmentFlag.AlignCenter), theme.badge_glyph(kind))

        return self._pixmap(("badge", kind), bw, bh, draw)

    def _expander_pixmap(self, state: int, color: QColor) -> QPixmap:
        w, h = self._indent, self._row_h

        def draw(q: QPainter) -> None:
            pen = QPen(color, max(1.3, w / 11), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                       Qt.PenJoinStyle.RoundJoin)
            q.setPen(pen)
            q.setBrush(Qt.BrushStyle.NoBrush)
            cx, cy = w / 2, h / 2
            s = max(2.5, w * 0.17)
            if state == 1:
                q.drawPolyline(QPolygonF([QPointF(cx - s / 2, cy - s), QPointF(cx + s / 2, cy),
                                          QPointF(cx - s / 2, cy + s)]))
            elif state == 2:
                q.drawPolyline(QPolygonF([QPointF(cx - s, cy - s / 2), QPointF(cx, cy + s / 2),
                                          QPointF(cx + s, cy - s / 2)]))
            else:
                q.drawArc(QRectF(cx - s, cy - s, 2 * s, 2 * s), 90 * 16, -270 * 16)

        return self._pixmap(("exp", state, color.rgba()), w, h, draw)

    def paintEvent(self, event) -> None:  # noqa: N802
        vp = self.viewport()
        p = QPainter(vp)
        try:
            self._paint(p, event.rect(), vp.width(), vp.height())
        finally:
            p.end()

    def _paint(self, p: QPainter, clip: QRect, w: int, h: int) -> None:
        c = self._palette_colors()
        p.fillRect(clip, c.base)
        if self._rows <= 0 or self._source is None:
            if self._placeholder:
                p.setPen(c.muted)
                p.setFont(self.font())
                p.drawText(QRect(16, 0, max(0, w - 32), h),
                           int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), self._placeholder)
            return
        rh = self._row_h
        top = self._top
        first = top + max(0, clip.top()) // rh
        last = min(self._rows - 1, top + max(0, clip.bottom()) // rh)
        if last < first:
            return
        gw = self._gutter_w
        fc = self._fc
        scroll_left = self._scroll_left()
        xs = self._xs
        hoff = self._hoff
        cols = []
        for i, col in enumerate(self._cols):
            if not col.visible or i == fc:
                continue
            x = gw + xs[i] - hoff
            cw = col.width
            if x + cw <= scroll_left or x >= w or x + cw <= clip.left() or x > clip.right():
                continue
            cols.append((i, col, x, cw))
        cells_mode = self._mode == "cells"
        sel = c.sel if self.hasFocus() else c.sel_inactive
        hover = self._hover
        alternating = self._alternating
        rows = range(first, last + 1)
        marked = self._selected_rows_in(first, last)    # rows with a selection (once per paint)

        def row_bg(row: int) -> QColor | None:
            if not cells_mode and row in marked:
                return sel
            if row == hover:
                return c.hover
            if alternating and row & 1:
                return c.alt
            return None

        # 1. row backgrounds
        for row in rows:
            bg = row_bg(row)
            if bg is not None:
                p.fillRect(gw, (row - top) * rh, w - gw, rh, bg)
        # 2. scrollable cells
        p.setFont(self._font)
        self._pfont = 0
        for row in rows:
            y = (row - top) * rh
            for i, col, x, cw in cols:
                if cells_mode and self._cell_selected(row, i):
                    p.fillRect(x, y, cw, rh, sel)
                self._paint_cell(p, c, row, i, col, x, y, cw)
        if self._grid_lines:
            p.setPen(c.line)
            bottom = (last - top + 1) * rh - 1
            for _i, _col, x, cw in cols:
                p.drawLine(x + cw - 1, 0, x + cw - 1, bottom)
        # 3. frozen column on top
        if fc >= 0:
            col = self._cols[fc]
            fw = col.width
            for row in rows:
                y = (row - top) * rh
                bg = row_bg(row)
                if cells_mode and self._cell_selected(row, fc):
                    bg = sel
                p.fillRect(gw, y, fw, rh, bg if bg is not None else c.base)
                self._paint_cell(p, c, row, fc, col, gw, y, fw)
            p.setPen(c.line)
            p.drawLine(gw + fw - 1, 0, gw + fw - 1, h)
        if self._grid_lines:
            p.setPen(c.line)
            right = min(w, self._content_right())
            for row in rows:
                y = (row - top) * rh + rh - 1
                p.drawLine(gw, y, right, y)
        # 4. gutter
        if gw:
            self._paint_gutter(p, c, rows, gw, h, sel, marked)
        # 5. outline of the current cell / row (softer without focus, but still clearly visible)
        cur = self._cur_row
        if first <= cur <= last:
            if cells_mode and self._col_ok(self._cur_col):
                rect = self.cell_rect(cur, self._cur_col)
            else:
                rect = QRect(gw, (cur - top) * rh, max(0, min(w, max(self._content_right(), gw + 1)) - gw), rh)
            if rect.isValid():
                p.setPen(c.focus if self.hasFocus() else c.focus_inactive)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(rect.adjusted(0, 0, -1, -1))

    def _selected_rows_in(self, first: int, last: int) -> set[int]:
        """Rows in [first, last] touched by the selection (cells mode: by any selected cell)."""
        if self._mode == "single":
            return {self._cur_row} if first <= self._cur_row <= last else set()
        out: set[int] = set()
        for r0, r1, _c0, _c1 in self._ranges:
            lo, hi = max(r0, first), min(r1, last)
            if lo <= hi:
                out.update(range(lo, hi + 1))
        return out

    def _paint_gutter(self, p: QPainter, c: _Colors, rows: range, gw: int, h: int, sel: QColor,
                      marked: set[int]) -> None:
        top, rh = self._top, self._row_h
        p.fillRect(0, 0, gw, h, c.gutter)
        fn = getattr(self._source, "row_label", None)
        if self._pfont != 0:
            p.setFont(self._font)
            self._pfont = 0
        fm = self._fm
        for row in rows:
            y = (row - top) * rh
            if row in marked:
                p.fillRect(0, y, gw, rh, sel)
            if fn is None:
                continue
            try:
                label = fn(row)
            except Exception:  # noqa: BLE001
                label = None
            if label:
                p.setPen(c.text if row == self._cur_row else c.muted)
                p.drawText(gw - self._pad_x - fm.horizontalAdvance(label), y + self._text_dy, label)
        p.setPen(c.line)
        p.drawLine(gw - 1, 0, gw - 1, h)

    def _paint_cell(self, p: QPainter, c: _Colors, row: int, index: int, col: Column, x: int, y: int,
                    cw: int) -> None:
        cell = self._fetch(row, col)
        if cell is None:
            if self._pfont != 0:
                p.setFont(self._font)
                self._pfont = 0
            p.setPen(c.faint)
            p.drawText(x + self._pad_x, y + self._text_dy, PLACEHOLDER)
            return
        tx, right = self._text_area(cell, index, x, cw)
        rh = self._row_h
        if index == self._tree_col:
            left = x + self._pad_x
            depth = cell.depth
            indent = self._indent
            if depth > 0:
                p.setPen(c.guide)
                half = indent // 2
                for k in range(depth):
                    gx = left + k * indent + half
                    if gx >= right:
                        break
                    p.drawLine(gx, y, gx, y + rh - 1)
            ex = left + depth * indent
            if cell.expander and ex + indent <= right + self._pad_x:
                p.drawPixmap(ex, y, self._expander_pixmap(cell.expander, c.muted))
        if cell.badge:
            bx = tx - self._badge_w - self._gap
            if bx + self._badge_w <= right:
                p.drawPixmap(bx, y + (rh - self._badge_h) // 2, self._badge_pixmap(cell.badge))
        avail = right - tx
        text = cell.text
        if avail <= 0 or not text:
            return
        style = (1 if cell.bold else 0) + (2 if cell.italic else 0)
        if style != self._pfont:
            p.setFont(self._fonts[style])
            self._pfont = style
        fm = self._fms[style]
        shown, kept = self._elide(fm, text, avail)
        if not shown:
            return
        align = cell.align if cell.align != "left" else col.align
        if align != "left":
            tw = fm.horizontalAdvance(shown)
            if align == "right":
                tx = right - tw
            elif align == "center":
                tx += max(0, (avail - tw) // 2)
        if cell.spans:
            pad = max(1, self._pad_y - 2)
            for span in cell.spans:
                s = max(0, int(span[0]))
                e = min(int(span[1]), kept)
                if s >= e:
                    continue
                sx = tx + fm.horizontalAdvance(shown[:s])
                ex = tx + fm.horizontalAdvance(shown[:e])
                color = c.current_match if len(span) > 2 and span[2] else c.match
                p.fillRect(sx, y + pad, max(1, ex - sx), rh - 2 * pad, color)
        role = cell.role
        p.setPen(c.roles.get(role, c.text) if role else c.text)
        p.drawText(tx, y + self._text_dy, shown)


def _clamp_f(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


# ---------------------------------------------------------------------------------
# header
# ---------------------------------------------------------------------------------
class GridHeader(QWidget):
    """Column header of a :class:`VirtualGrid`: titles, sort indicators, resize (drag an edge,
    double-click an edge = fit), reorder (drag a section), click / context menu signals
    (emitted by the grid). Shares the grid viewport's x axis."""

    EDGE = 4             # px around a section's right edge that start a resize
    AUTO_SCROLL = 24     # px from the edges where a section drag scrolls horizontally

    def __init__(self, grid: VirtualGrid) -> None:
        super().__init__(grid)
        self._grid = grid
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._press: tuple | None = None    # ("resize", col, x0, w0) | ("section", col, x0, grab_dx)
        self._dragging = False
        self._drop = -1                     # insertion position while dragging a section
        self._mouse_x = 0
        self._hover_col = -1
        self._auto = QTimer(self)
        self._auto.setInterval(30)
        self._auto.timeout.connect(self._auto_tick)

    # ---------------------------------------------------------------- geometry
    def sizeHint(self) -> QSize:  # noqa: N802
        fm = self.fontMetrics()
        return QSize(100, fm.height() + 2 * max(4, fm.height() // 3))

    def _pad(self) -> int:
        return max(6, self.fontMetrics().averageCharWidth())

    def section_rect(self, index: int) -> QRect:
        """Header-local rect of column ``index`` (null when hidden)."""
        g = self._grid
        if not g._col_ok(index):
            return QRect()
        return QRect(g._section_x(index), 0, g._cols[index].width, self.height())

    def _sorted_count(self) -> int:
        return sum(1 for col in self._grid._cols if col.sort_order)

    def _sort_area(self, col: Column) -> int:
        if not col.sort_order:
            return 0
        width = 8 + 4
        if col.sort_priority > 0 and self._sorted_count() > 1:
            width += self._small_fm().horizontalAdvance(str(col.sort_priority)) + 2
        return width

    def _small_font(self) -> QFont:
        font = QFont(self.font())
        font.setPointSizeF(max(_MIN_POINT_SIZE, VirtualGrid._point_size(font) * 0.8))
        return font

    def _small_fm(self) -> QFontMetrics:
        return QFontMetrics(self._small_font())

    def title_width(self, index: int) -> int:
        col = self._grid._cols[index]
        return self.fontMetrics().horizontalAdvance(col.title) + 2 * self._pad() + self._sort_area(col) + 1

    def _on_screen(self) -> list[int]:
        """Visible columns that are (partly) on screen, in display order."""
        g = self._grid
        left = g._scroll_left()
        out = []
        for i, col in enumerate(g._cols):
            if not col.visible:
                continue
            if i == g._fc:
                out.append(i)
                continue
            x = g._section_x(i)
            if x + col.width > left and x < self.width():
                out.append(i)
        return out

    def edge_at(self, x: int) -> int:
        """Column whose right edge is within :attr:`EDGE` px of ``x`` (-1: none)."""
        g = self._grid
        left = g._scroll_left()
        best, best_d = -1, self.EDGE + 1
        for i in self._on_screen():
            ex = g._section_x(i) + g._cols[i].width
            if i != g._fc and ex < left:
                continue
            d = abs(x - ex)
            if d <= self.EDGE and d < best_d:
                best, best_d = i, d
        return best

    def _drop_position(self, x: int) -> int:
        g = self._grid
        shown = self._on_screen()
        left = g._scroll_left()
        for i in shown:
            sx = g._section_x(i) if i == g._fc else max(g._section_x(i), left)
            ex = g._section_x(i) + g._cols[i].width
            if x < (sx + ex) // 2:
                return i
        return (shown[-1] + 1) if shown else len(g._cols)

    def _drop_x(self, pos: int) -> int:
        g = self._grid
        if pos < len(g._cols) and g._col_ok(pos):
            return g._section_x(pos) if pos == g._fc else max(g._section_x(pos), g._scroll_left())
        prev = [i for i in range(min(pos, len(g._cols)) - 1, -1, -1) if g._col_ok(i)]
        if prev:
            return g._section_x(prev[0]) + g._cols[prev[0]].width
        return g._gutter_w

    # ---------------------------------------------------------------- mouse
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        x = event.position().toPoint().x()
        self._mouse_x = x
        if event.button() != Qt.MouseButton.LeftButton:
            event.accept()
            return
        g = self._grid
        edge = self.edge_at(x)
        if edge >= 0:
            self._press = ("resize", edge, x, g._cols[edge].width)
        else:
            col = g.column_at(x)
            self._press = ("section", col, x, x - g._section_x(col)) if col >= 0 else None
        self._dragging = False
        self.update()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        x = event.position().toPoint().x()
        self._mouse_x = x
        g = self._grid
        press = self._press
        if press is None:
            edge = self.edge_at(x)
            self.setCursor(Qt.CursorShape.SplitHCursor if edge >= 0 else Qt.CursorShape.ArrowCursor)
            hover = g.column_at(x) if edge < 0 else -1
            if hover != self._hover_col:
                self._hover_col = hover
                self.update()
        elif press[0] == "resize":
            g._set_width(press[1], press[3] + x - press[2])
        else:
            if not self._dragging and abs(x - press[2]) >= QApplication.startDragDistance():
                self._dragging = True
            if self._dragging:
                self._drop = self._drop_position(x)
                near = x < g._scroll_left() + self.AUTO_SCROLL or x > self.width() - self.AUTO_SCROLL
                if near and not self._auto.isActive():
                    self._auto.start()
                elif not near:
                    self._auto.stop()
                self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        press, dragging = self._press, self._dragging
        self._press = None
        self._dragging = False
        self._auto.stop()
        g = self._grid
        if press is not None and event.button() == Qt.MouseButton.LeftButton:
            if press[0] == "resize":
                if g._cols[press[1]].width != press[3]:
                    g.columns_changed.emit()
            elif dragging:
                src, pos = press[1], self._drop
                dst = pos if pos <= src else pos - 1
                dst = _clamp(dst, 0, len(g._cols) - 1)
                if dst != src:
                    g.move_column(src, dst)
            else:
                g.header_clicked.emit(press[1], event.modifiers())
        self._drop = -1
        self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        x = event.position().toPoint().x()
        if event.button() == Qt.MouseButton.LeftButton:
            edge = self.edge_at(x)
            if edge >= 0:
                self._grid.fit_column(edge)
        self._press = None
        event.accept()

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover_col != -1:
            self._hover_col = -1
            self.update()
        super().leaveEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802
        col = self._grid.column_at(event.pos().x())
        self._grid.header_context_menu.emit(col, event.globalPos())
        event.accept()

    def _auto_tick(self) -> None:
        if not self._dragging:
            self._auto.stop()
            return
        g = self._grid
        step = max(8, self.fontMetrics().averageCharWidth() * 2)
        if self._mouse_x < g._scroll_left() + self.AUTO_SCROLL:
            g._set_hoff(g._hoff - step)
        elif self._mouse_x > self.width() - self.AUTO_SCROLL:
            g._set_hoff(g._hoff + step)
        self._drop = self._drop_position(self._mouse_x)
        self.update()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            text = self.tooltip_at(event.pos().x())
            if text:
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

    def tooltip_at(self, x: int) -> str | None:
        """Column tooltip, else the title when elided, plus the sort state."""
        g = self._grid
        index = g.column_at(x)
        if index < 0:
            return None
        col = g._cols[index]
        parts = []
        if col.tooltip:
            parts.append(col.tooltip)
        elif self.title_width(index) > col.width:
            parts.append(col.title)
        if col.sort_order:
            text = t("gui.grid.sort.asc") if col.sort_order > 0 else t("gui.grid.sort.desc")
            if col.sort_priority > 0 and self._sorted_count() > 1:
                text += " (" + t("gui.grid.sort.key", n=col.sort_priority) + ")"
            parts.append(text)
        return "\n".join(parts) or None

    # ---------------------------------------------------------------- painting
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        try:
            self._paint(p)
        finally:
            p.end()

    def _paint(self, p: QPainter) -> None:
        g = self._grid
        c = g._palette_colors()
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, c.header_bg)
        p.setFont(self.font())
        sorted_count = self._sorted_count()
        fc = g._fc
        for i in self._on_screen():
            if i != fc:
                self._paint_section(p, c, i, g._section_x(i), h, sorted_count)
        if fc >= 0:
            fw = g._cols[fc].width
            p.fillRect(g._gutter_w, 0, fw, h, c.header_bg)
            self._paint_section(p, c, fc, g._gutter_w, h, sorted_count)
            p.setPen(c.line)
            p.drawLine(g._gutter_w + fw - 1, 0, g._gutter_w + fw - 1, h)
        if g._gutter_w:
            p.fillRect(0, 0, g._gutter_w, h, c.header_bg)
            p.setPen(c.line)
            p.drawLine(g._gutter_w - 1, 0, g._gutter_w - 1, h)
        p.setPen(c.line)
        p.drawLine(0, h - 1, w, h - 1)
        if self._dragging and self._press is not None:
            self._paint_drag(p, c, h)

    def _paint_section(self, p: QPainter, c: _Colors, index: int, x: int, h: int, sorted_count: int) -> None:
        g = self._grid
        col = g._cols[index]
        cw = col.width
        pressed = self._press is not None and self._press[0] == "section" and self._press[1] == index
        if pressed or index == self._hover_col:
            p.fillRect(x, 0, cw, h - 1, c.header_hover)
        pad = self._pad()
        sort_w = self._sort_area(col)
        avail = cw - 2 * pad - sort_w
        fm = self.fontMetrics()
        if avail > 0 and col.title:
            title = fm.elidedText(col.title, Qt.TextElideMode.ElideRight, avail)
            tw = fm.horizontalAdvance(title)
            tx = x + pad
            if col.align == "right":
                tx = x + cw - pad - sort_w - tw
            elif col.align == "center":
                tx = x + pad + max(0, (avail - tw) // 2)
            p.setPen(c.header_text)
            p.drawText(tx, (h - fm.height()) // 2 + fm.ascent(), title)
        if col.sort_order and cw > 2 * pad:
            ax = x + cw - pad - 8
            cy = h / 2
            p.save()
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c.accent)
            if col.sort_order > 0:
                tri = [QPointF(ax, cy + 2.5), QPointF(ax + 8, cy + 2.5), QPointF(ax + 4, cy - 2.5)]
            else:
                tri = [QPointF(ax, cy - 2.5), QPointF(ax + 8, cy - 2.5), QPointF(ax + 4, cy + 2.5)]
            p.drawPolygon(QPolygonF(tri))
            p.restore()
            if col.sort_priority > 0 and sorted_count > 1:
                small = self._small_font()
                sfm = QFontMetrics(small)
                label = str(col.sort_priority)
                p.setFont(small)
                p.setPen(c.muted)
                p.drawText(ax - 2 - sfm.horizontalAdvance(label), (h - sfm.height()) // 2 + sfm.ascent(), label)
                p.setFont(self.font())
        p.setPen(c.line)
        p.drawLine(x + cw - 1, 4, x + cw - 1, h - 5)

    def _paint_drag(self, p: QPainter, c: _Colors, h: int) -> None:
        g = self._grid
        src = self._press[1]
        if not g._col_ok(src):
            return
        col = g._cols[src]
        ghost = QRect(self._mouse_x - self._press[3], 0, col.width, h - 1)
        tint = QColor(c.accent)
        tint.setAlpha(40)
        p.fillRect(ghost, tint)
        p.setPen(c.accent)
        p.drawRect(ghost.adjusted(0, 0, -1, -1))
        fm = self.fontMetrics()
        pad = self._pad()
        title = fm.elidedText(col.title, Qt.TextElideMode.ElideRight, max(0, col.width - 2 * pad))
        p.setPen(c.header_text)
        p.drawText(ghost.x() + pad, (h - fm.height()) // 2 + fm.ascent(), title)
        if self._drop >= 0:
            x = self._drop_x(self._drop)
            p.fillRect(x - 1, 0, 3, h, c.accent)

    def drop_position(self) -> int:
        """Insertion position of the section being dragged (-1 when not dragging)."""
        return self._drop if self._dragging else -1
