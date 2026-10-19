"""RawView: the raw bytes of the data file, page by page (docs/ARCHITECTURE.md §11.7 "Raw";
AGENT_PROMPT §4.3 second bullet).

The data file (``doc.data_path``: the source itself, or the prepared UTF-8 copy of a gzip /
UTF-16 input) is never read whole: 64 KiB pages (:data:`PAGE_BYTES`) are read by one
background reader per view (:class:`PageFetcher`: the newest wanted pages first, results
handed to the GUI thread at once by a queued signal -- no task-poll latency, so a scrollbar
drag shows data for the positions it passes) and kept in a bounded LRU (:data:`MAX_PAGES`
pages = 1 MiB). The GUI thread reads no file data at all; it only splits cached pages into
rows, and a long row scroll walks at most what the cache holds per step (the rest of the
move stays pending).

* :class:`RawArea` -- the painted scroll area: rows are text lines cut into bounded visual
  rows by the rule of :mod:`jsonreshape.gui.rawdata` (lines start after ``\\n``; long lines
  -- a 5 GB minified file is one line -- are cut on character boundaries into rows as wide
  as the window: the row width in bytes follows the visible text width, at least
  :data:`~rawdata.MIN_ROW_BYTES`; a resize or zoom keeps the first shown byte on top). The
  vertical scrollbar maps to **byte offsets** of the whole file
  (scaled to ``2**30`` when needed): dragging reaches any part at once; wheel / keys move
  rows, also upwards across page boundaries (the preceding page is read and split by the
  same rule, so rows are identical in both directions). Gutter: byte offset of every row
  (decimal / hex) and the line number when it is known (:class:`LineIndex` or the file
  start). Control characters are drawn as muted symbols, invalid bytes as a red U+FFFD.
  Highlights: the current node's span, the selection, quick-find matches, the JSON error
  byte (red).
* :class:`RawView` -- toolbar (go to offset / line, hex gutter, line numbers, go to the
  JSON error, a note when a prepared UTF-8 copy is shown, position), an info bar, the
  area and the quick find bar (matches in the loaded pages only).

Public API used by the document tab: :meth:`RawView.goto_offset` (JSON error position,
"open in raw view" of the text view) and :meth:`RawView.goto_line` (1-based).

Edits (docs/ARCHITECTURE.md §12.9 "Raw"): the view always shows the original data file. The
current node is highlighted (and jumped to) only when its span is a file offset; a node in
the edit layer (``is_edit_offset``: edited or new) gets the note "edited -- the raw view shows
the original file" instead. On ``session.data_changed`` the session's (remapped) current node
is looked up again without moving the view. A node is resolved in the document state pinned
when it was asked for (:func:`~jsonreshape.gui.widgets.task_doc`), so an edit applied before the
worker starts never makes it read another state (review CL-9).
"""

from __future__ import annotations

import bisect
import codecs
import logging
import re
import threading
import weakref
from collections import OrderedDict
from typing import Any, Callable

from PySide6.QtCore import QObject, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QAction,
    QColor,
    QContextMenuEvent,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPalette,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSlider,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jsonreshape.errors import FileChangedError, JsonReshapeError
from jsonreshape.gui import theme
from jsonreshape.gui.quickfind import QuickFindBar, status_text
from jsonreshape.gui.rawdata import (
    DEFAULT_RULE,
    MIN_ROW_BYTES,
    LineIndex,
    RowRule,
    decode_row,
    display_text,
    is_line_start,
    line_index_for,
    need_back,
    need_forward,
    row_start_at,
    rows_back,
    walk_rows,
)
from jsonreshape.gui.widgets import (
    InfoBar,
    ViewBase,
    edited_span,
    format_count,
    format_size,
    mono_font,
    set_clipboard_text,
    task_doc,
    task_node,
)
from jsonreshape.i18n import t
from jsonreshape.tasks import CancelToken

log = logging.getLogger(__name__)

__all__ = [
    "AUTO_LINE_INDEX_BYTES",
    "COPY_CAP",
    "MAX_PAGES",
    "PAGE_BYTES",
    "LineIndex",
    "PageFetcher",
    "PageStore",
    "RawArea",
    "RawView",
    "parse_line",
    "parse_offset",
    "read_pages",
]

PAGE_BYTES = 64 * 1024            # one page of the data file
MAX_PAGES = 16                    # page cache: 1 MiB
READ_BATCH = 8                    # pages per read
#: A row scroll walks at most what this many cached pages hold per step (the rest stays pending).
WALK_PAGES = MAX_PAGES // 2 - 1
COPY_CAP = 1 << 20                # copying a selection copies at most 1 MiB
SELECTED_TEXT_MAX = 64 * 1024     # selected_text() (search prefill) at most
FIND_MAX_MATCHES = 10_000
AUTO_LINE_INDEX_BYTES = 32 << 20  # smaller data files get line numbers automatically
INDEX_POLL_MS = 150
_SCALE = 1 << 30                  # scaled scrollbar range for files larger than a C int
_INT_MAX = (1 << 31) - 1
_MIN_POINT = 6.0
_MAX_POINT = 40.0

_SPECIAL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\ufeff\u200b\ufffd]")


# ==================================================================================
# input parsing
# ==================================================================================
def parse_offset(text: str) -> int:
    """A byte offset typed by the user: decimal (``123456``, ``123 456``, ``123_456``) or
    hexadecimal (``0x1E240``). ``ValueError`` otherwise (negative numbers included)."""
    s = re.sub(r"[\s_,']", "", text or "")
    if not s:
        raise ValueError("empty offset")
    if s[:2].lower() == "0x":
        digits = s[2:]
        if not digits or not re.fullmatch(r"[0-9a-fA-F]+", digits):
            raise ValueError(f"invalid hexadecimal offset: {text!r}")
        return int(digits, 16)
    if not re.fullmatch(r"[0-9]+", s):
        raise ValueError(f"invalid offset: {text!r}")
    return int(s)


def parse_line(text: str) -> int:
    """A 1-based line number typed by the user (``ValueError`` when not a positive integer)."""
    s = re.sub(r"[\s_,']", "", text or "")
    if not re.fullmatch(r"[0-9]+", s) or int(s) < 1:
        raise ValueError(f"invalid line number: {text!r}")
    return int(s)


# ==================================================================================
# reading (worker threads only)
# ==================================================================================
def read_pages(doc: Any, pages: list[int], cancel: CancelToken | None = None, *,
               page_bytes: int = PAGE_BYTES) -> dict[int, bytes]:
    """Pages ``pages`` of ``doc``'s data file (``{page: bytes}``; the last page may be
    shorter). Worker threads only: the only function of this module that reads pages."""
    size = int(doc.data_size)
    out: dict[int, bytes] = {}
    with doc.open_data(buffering=0) as f:
        for p in sorted(set(pages)):
            if cancel is not None:
                cancel.check()
            start = p * page_bytes
            if start >= size:
                continue
            want = min(page_bytes, size - start)
            f.seek(start)
            data = f.read(want)
            if data is None or len(data) < want:
                raise FileChangedError(path=str(doc.source_path))
            out[p] = bytes(data)
    return out


def read_range(doc: Any, start: int, end: int) -> bytes:
    """Bytes ``[start, end)`` of the data file (worker threads only; callers bound the size)."""
    end = min(end, int(doc.data_size))
    if end <= start:
        return b""
    with doc.open_data(buffering=0) as f:
        f.seek(start)
        data = f.read(end - start) or b""
    return bytes(data)


# ==================================================================================
# the reader thread
# ==================================================================================
class _FetchState:
    """What the reader thread shares with the GUI thread (kept apart from the QObject so the
    thread never keeps the view alive)."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.job: tuple[int, Any, list[int]] | None = None      # (generation, document, pages)
        self.taken = 0                    # jobs taken by the thread whose results are not delivered
        self.closed = False
        self.thread: threading.Thread | None = None
        self.emit: Any = None

    def shutdown(self, *_args: Any) -> None:
        with self.cond:
            self.closed = True
            self.job = None
            self.cond.notify_all()


def _fetch_worker(state: _FetchState) -> None:
    while True:
        with state.cond:
            while not state.closed and state.job is None:
                state.cond.wait()
            if state.closed:
                return
            gen, doc, pages = state.job
            state.job = None
            state.taken += 1
        result: dict[int, bytes] | None = None
        error: BaseException | None = None
        try:
            result = read_pages(doc, pages)
        except Exception as exc:  # noqa: BLE001 - reported to the view
            error = exc
        if not state.emit(gen, result, error):
            return


class PageFetcher(QObject):
    """Reads pages of a data file on one daemon thread, the newest request first (a request
    not started yet is replaced by a newer one); ``arrived(gen, pages | None, error | None)``
    is delivered in the GUI thread through a queued signal as soon as the read ends."""

    arrived = Signal(object, object, object)
    _deliver = Signal(object, object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        state = _FetchState()
        self._state = state
        me = weakref.ref(self)

        def emit(gen: int, result: Any, error: Any) -> bool:   # reader thread
            obj = me()
            if obj is None or state.closed:
                state.shutdown()
                return False
            try:
                obj._deliver.emit(gen, result, error)
            except RuntimeError:          # the C++ object is gone
                state.shutdown()
                return False
            return True

        state.emit = emit
        self._deliver.connect(self._on_deliver, Qt.ConnectionType.QueuedConnection)
        self.destroyed.connect(state.shutdown)

    @Slot(object, object, object)
    def _on_deliver(self, gen: Any, result: Any, error: Any) -> None:
        with self._state.cond:
            self._state.taken = max(0, self._state.taken - 1)
        if not self._state.closed:
            self.arrived.emit(gen, result, error)

    def submit(self, gen: int, doc: Any, pages: list[int]) -> None:
        """Read ``pages`` (replacing a request the thread has not started yet)."""
        state = self._state
        with state.cond:
            if state.closed:
                return
            state.job = (gen, doc, list(pages))
            if state.thread is None or not state.thread.is_alive():
                state.thread = threading.Thread(target=_fetch_worker, args=(state,), name="jsonreshape-raw-pages",
                                                daemon=True)
                state.thread.start()
            state.cond.notify()

    def queued(self) -> bool:
        """A request waits for the thread."""
        return self._state.job is not None

    def idle(self) -> bool:
        """Nothing queued, read or undelivered."""
        state = self._state
        with state.cond:
            return state.job is None and state.taken == 0

    def drop_queued(self) -> None:
        with self._state.cond:
            self._state.job = None

    def close(self) -> None:
        self._state.shutdown()


# ==================================================================================
# page cache
# ==================================================================================
class PageStore:
    """Bounded LRU of file pages (GUI thread). :meth:`window` returns cached bytes or
    records the missing pages as *urgent*; :meth:`prefetch` records missing pages of
    lower priority; the owner reads :meth:`take` pages in a task and :meth:`put` s them."""

    def __init__(self, size: int, *, page_bytes: int = PAGE_BYTES, capacity: int = MAX_PAGES) -> None:
        self.size = int(size)
        self.page = int(page_bytes)
        self.capacity = max(4, int(capacity))
        self._pages: OrderedDict[int, bytes] = OrderedDict()
        self.urgent: list[int] = []
        self.low: list[int] = []

    def __len__(self) -> int:
        return len(self._pages)

    def clear(self) -> None:
        self._pages.clear()
        self.urgent = []
        self.low = []

    def cached(self) -> list[int]:
        return list(self._pages)

    def nbytes(self) -> int:
        return sum(len(v) for v in self._pages.values())

    def _missing(self, lo: int, hi: int) -> list[int]:
        lo = max(0, lo)
        hi = min(self.size, hi)
        if hi <= lo:
            return []
        return [p for p in range(lo // self.page, (hi - 1) // self.page + 1) if p not in self._pages]

    def has(self, lo: int, hi: int) -> bool:
        return not self._missing(lo, hi)

    def window(self, lo: int, hi: int) -> tuple[bytes, int] | None:
        """``(bytes [lo, hi), lo)`` clamped to the file, or ``None`` (missing pages become
        the urgent request, replacing the previous one)."""
        lo = max(0, lo)
        hi = min(self.size, hi)
        if hi <= lo:
            return b"", lo
        missing = self._missing(lo, hi)
        if missing:
            self.urgent = missing
            return None
        first, last = lo // self.page, (hi - 1) // self.page
        parts = []
        for p in range(first, last + 1):
            self._pages.move_to_end(p)
            parts.append(self._pages[p])
        data = parts[0] if len(parts) == 1 else b"".join(parts)
        off = lo - first * self.page
        return data[off:off + (hi - lo)], lo

    def prefetch(self, lo: int, hi: int) -> None:
        """Remember missing pages of ``[lo, hi)`` as low-priority wishes (replacing older ones)."""
        self.low = [p for p in self._missing(lo, hi) if p not in self.urgent][: self.capacity // 2]

    def take(self, n: int) -> list[int]:
        """Up to ``n`` wanted pages, urgent first (removed from the wish lists)."""
        out = []
        for lst in (self.urgent, self.low):
            while lst and len(out) < n:
                p = lst.pop(0)
                if p not in self._pages and p not in out:
                    out.append(p)
        return out

    def wanted(self) -> bool:
        return bool(self.urgent or self.low)

    def put(self, pages: dict[int, bytes]) -> None:
        for p, data in pages.items():
            self._pages[p] = data
            self._pages.move_to_end(p)
        while len(self._pages) > self.capacity:
            self._pages.popitem(last=False)
        self.urgent = [p for p in self.urgent if p not in self._pages]
        self.low = [p for p in self.low if p not in self._pages]

    def region_around(self, x: int, limit: int = MAX_PAGES * PAGE_BYTES) -> tuple[int, bytes] | None:
        """The run of consecutive cached pages containing offset ``x`` (at most ``limit``
        bytes): ``(start offset, bytes)`` or ``None``."""
        p = x // self.page if self.size else 0
        if p not in self._pages:
            return None
        lo = hi = p
        max_pages = max(1, limit // self.page)
        while lo - 1 in self._pages and hi - lo + 1 < max_pages:
            lo -= 1
        while hi + 1 in self._pages and hi - lo + 1 < max_pages:
            hi += 1
        return lo * self.page, b"".join(self._pages[k] for k in range(lo, hi + 1))


# ==================================================================================
# colors
# ==================================================================================
def _blend(a: QColor, b: QColor, f: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * f), round(a.green() + (b.green() - a.green()) * f),
                  round(a.blue() + (b.blue() - a.blue()) * f))


class _Colors:
    __slots__ = ("base", "text", "gutter", "muted", "faint", "line", "sel", "node", "match", "current_match",
                 "error", "error_bg", "invalid")

    def __init__(self, pal: QPalette) -> None:
        cr = QPalette.ColorRole
        self.base = pal.color(cr.Base)
        self.text = pal.color(cr.Text)
        highlight = pal.color(cr.Highlight)
        dark = self.base.lightness() < 128
        self.gutter = _blend(self.base, pal.color(cr.Window), 0.7)
        self.muted = QColor(theme.color("muted"))
        self.faint = QColor(theme.color("faint"))
        self.line = QColor(theme.color("line"))
        self.sel = _blend(self.base, highlight, 0.42 if dark else 0.30)
        self.node = QColor(theme.color("accent_soft"))
        self.match = QColor(theme.color("match"))
        self.current_match = QColor(theme.color("current_match"))
        self.error = QColor(theme.color("danger"))
        self.error_bg = _blend(self.base, self.error, 0.45 if dark else 0.30)
        self.invalid = QColor(theme.color("danger"))


# ==================================================================================
# the painted area
# ==================================================================================
class RawArea(QAbstractScrollArea):
    """Painted rows of the data file (see the module docstring). Created by
    :class:`RawView`, which owns the page reading."""

    position_changed = Signal()        # top / shown rows changed
    selection_changed = Signal()
    settled = Signal()                 # the requested position is shown with data

    def __init__(self, view: RawView) -> None:
        super().__init__(view)
        self._view = view
        self.store = PageStore(0)
        self.size = 0
        self._top = 0
        self._nav: tuple | None = None        # pending navigation
        self._waiting = False                 # rows / navigation wait for pages
        self._rows: list[int] = []            # boundaries of the shown rows
        self._buf = b""
        self._base = 0
        self._end: tuple[int, int] | None = None   # (page rows, end top)
        self._end_wanted = False              # the end top was known, then went stale: read it again
        self._rule: RowRule = DEFAULT_RULE    # row width follows the viewport (_fit_rule)
        self._anchor = -1                     # selection anchor / cursor (byte offsets)
        self._cursor = -1
        self._node_span: tuple[int, int] | None = None
        self._error_at: int | None = None
        self._matches: list[tuple[int, int]] = []
        self._match_cur: int | None = None
        self._hex = False
        self._line_anchor: tuple[int, int] | None = None   # (offset, line containing it)
        self._hoff = 0
        self._content_w = 0
        self._wheel = 0.0
        self.moves = 0                        # navigations so far (explicit jumps, scrolling)
        self._syncing = False
        self._decoded: dict[tuple[int, int], tuple[str, list[int] | None, str]] = {}
        self._colors: _Colors | None = None
        self._drag = False
        self._drag_pos = QPoint()
        self._auto = QTimer(self)
        self._auto.setInterval(50)
        self._auto.timeout.connect(self._auto_scroll)

        vp = self.viewport()
        vp.setAutoFillBackground(False)
        vp.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        vp.setCursor(Qt.CursorShape.IBeamCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        vbar = self.verticalScrollBar()
        vbar.actionTriggered.connect(self._on_vbar_action)
        vbar.valueChanged.connect(self._on_vbar_value)
        self.horizontalScrollBar().valueChanged.connect(self._on_hbar_value)
        theme.notifier().theme_changed.connect(self._on_theme_changed)

        font = mono_font(view.session)
        self._base_font = QFont(font)
        self._base_size = font.pointSizeF() if font.pointSizeF() > 0 else 10.0
        self._zoom = 0.0
        self._apply_font(font)

    # ------------------------------------------------------------------ state
    def reset(self, size: int) -> None:
        """A new data file of ``size`` bytes (everything forgotten)."""
        self.size = max(0, int(size))
        self.store = PageStore(self.size)
        self._top = 0
        self._nav = None
        self._waiting = self.size > 0
        self._rows = []
        self._buf = b""
        self._base = 0
        self._end = None
        self._end_wanted = False
        self._anchor = self._cursor = -1
        self._node_span = None
        self._error_at = None
        self._matches = []
        self._match_cur = None
        self._line_anchor = None
        self._hoff = 0
        self._content_w = 0
        self._decoded.clear()
        self._sync_vbar()
        self._sync_hbar()
        self.viewport().update()

    def top(self) -> int:
        """Offset of the first shown row."""
        return self._top

    def rows(self) -> list[tuple[int, int]]:
        """The shown rows as ``(start, end)`` byte spans (empty while data is missing)."""
        r = self._rows
        return [(r[i], r[i + 1]) for i in range(len(r) - 1)]

    def is_settled(self) -> bool:
        """No navigation is waiting for data and the rows are computed."""
        return not self._waiting and self._nav is None and (self.size == 0 or len(self._rows) >= 2)

    def page_rows(self) -> int:
        return max(1, self.viewport().height() // self._row_h)

    def _window_rows(self) -> int:
        return max(1, -(-self.viewport().height() // self._row_h))

    def row_height(self) -> int:
        return self._row_h

    # ------------------------------------------------------------------ fonts
    def _apply_font(self, font: QFont) -> None:
        self._font = QFont(font)
        self._fm = QFontMetrics(self._font)
        self._fmf = QFontMetricsF(self._font)
        self._row_h = self._fm.lineSpacing() + 2
        self._cw = max(1.0, self._fmf.horizontalAdvance("0"))
        self._pad = max(4, round(self._cw * 0.75))
        self._forget_end()
        self._content_w = 0
        self._update_gutter()
        self._sync_hbar()
        self._resolve()
        self.viewport().update()

    def set_font(self, font: QFont, *, keep_zoom: bool = False) -> None:
        """A new data font (``keep_zoom``: the zoom steps stay applied on top of it)."""
        self._base_font = QFont(font)
        self._base_size = font.pointSizeF() if font.pointSizeF() > 0 else 10.0
        if not keep_zoom:
            self._zoom = 0.0
        zoomed = QFont(self._base_font)
        zoomed.setPointSizeF(min(_MAX_POINT, max(_MIN_POINT, self._base_size + self._zoom)))
        self._apply_font(zoomed)

    def zoom(self, steps: int) -> None:
        if steps == 0:
            self._zoom = 0.0
        else:
            size = min(_MAX_POINT, max(_MIN_POINT, self._base_size + self._zoom + steps))
            self._zoom = size - self._base_size
        font = QFont(self._base_font)
        font.setPointSizeF(self._base_size + self._zoom)
        self._apply_font(font)

    def font_point_size(self) -> float:
        return self._font.pointSizeF()

    # ------------------------------------------------------------------ gutter
    def set_hex(self, on: bool) -> None:
        self._hex = bool(on)
        self._update_gutter()
        self.viewport().update()

    def hex_offsets(self) -> bool:
        return self._hex

    def offset_text(self, offset: int) -> str:
        if self._hex:
            digits = max(4, len(f"{max(0, self.size - 1):X}"))
            return f"{offset:0{digits}X}"
        return str(offset)

    def _line_digits(self) -> int:
        if self._line_anchor is None:
            return 0
        idx = self._view.line_index
        if idx is not None and idx.complete and idx.line_count:
            return len(str(idx.line_count))
        return max(6, len(str(self._line_anchor[1])) + 1)

    def _update_gutter(self) -> None:
        digits = max(4, len(self.offset_text(max(0, self.size - 1))))
        self._off_w = round(self._cw * digits) + 2 * self._pad
        ld = self._line_digits()
        self._line_w = round(self._cw * ld) + 2 * self._pad if ld else 0
        self._gutter_w = self._off_w + self._line_w
        self._fit_rule()
        self._sync_hbar()

    def gutter_width(self) -> int:
        return self._gutter_w

    def rule(self) -> RowRule:
        """The row rule in use (its width ``w`` fits the visible text width)."""
        return self._rule

    def row_bytes(self) -> int:
        return self._rule.w

    def text_width(self) -> int:
        """Pixels available for the row text."""
        return max(0, self.viewport().width() - self._gutter_w - 2 * self._pad)

    def _fit_rule(self) -> None:
        """Rows as wide as the visible text (at least MIN_ROW_BYTES bytes); a new width
        keeps the row containing the first shown byte on top."""
        w = max(MIN_ROW_BYTES, int(self.text_width() / self._cw) - 1)
        rule = RowRule.for_width(w)
        if rule == self._rule:
            return
        self._rule = rule
        self._forget_end()
        self._decoded.clear()
        self._content_w = 0
        if self.size > 0 and self._nav is None:
            self._nav = ("top", self._top)     # the row containing the old first byte
            self._resolve()

    def set_line_anchor(self, offset: int, line: int) -> None:
        """Line ``line`` contains byte ``offset`` (line numbers for the rows near it)."""
        old = self._line_anchor
        self._line_anchor = (int(offset), int(line))
        if old is None or len(str(old[1])) != len(str(line)):
            self._update_gutter()
        self.viewport().update()
        self.position_changed.emit()

    def clear_line_anchor(self) -> None:
        if self._line_anchor is not None:
            self._line_anchor = None
            self._update_gutter()
            self.viewport().update()

    def line_anchor(self) -> tuple[int, int] | None:
        return self._line_anchor

    def anchor_usable(self) -> bool:
        """The line anchor lies in the bytes of the shown rows (line numbers computable)."""
        a = self._line_anchor
        return a is not None and self._base <= a[0] <= self._base + len(self._buf)

    def line_of_row(self, start: int) -> int | None:
        """Line number of the line containing ``start`` when it can be computed from the
        anchor and the bytes at hand."""
        a = self._line_anchor
        buf, base = self._buf, self._base
        if a is None or not (base <= a[0] <= base + len(buf)) or not (base <= start <= base + len(buf)):
            return None
        off, line = a
        if start >= off:
            return line + buf.count(b"\n", off - base, start - base)
        return line - buf.count(b"\n", start - base, off - base)

    # ------------------------------------------------------------------ highlights
    def set_node_span(self, span: tuple[int, int] | None) -> None:
        self._node_span = span
        self.viewport().update()

    def node_span(self) -> tuple[int, int] | None:
        return self._node_span

    def set_error_offset(self, offset: int | None) -> None:
        self._error_at = offset if offset is None else max(0, min(int(offset), max(0, self.size - 1)))
        self.viewport().update()

    def error_offset(self) -> int | None:
        return self._error_at

    def set_matches(self, matches: list[tuple[int, int]], current: int | None = None) -> None:
        self._matches = matches
        self._match_cur = current
        self.viewport().update()

    def matches(self) -> list[tuple[int, int]]:
        return self._matches

    def set_current_match(self, i: int | None) -> None:
        self._match_cur = i
        self.viewport().update()

    # ------------------------------------------------------------------ selection
    def selection(self) -> tuple[int, int] | None:
        """The selected byte span ``(start, end)`` or ``None``."""
        if self._anchor < 0 or self._cursor < 0 or self._anchor == self._cursor:
            return None
        return (min(self._anchor, self._cursor), max(self._anchor, self._cursor))

    def set_selection(self, start: int, end: int) -> None:
        start = max(0, min(int(start), self.size))
        end = max(start, min(int(end), self.size))
        self._anchor, self._cursor = start, end
        self.viewport().update()
        self.selection_changed.emit()

    def clear_selection(self) -> None:
        if self._anchor >= 0 or self._cursor >= 0:
            self._anchor = self._cursor = -1
            self.viewport().update()
            self.selection_changed.emit()

    def cursor_offset(self) -> int:
        return self._cursor

    # ------------------------------------------------------------------ navigation
    def goto(self, offset: int, *, rows_above: int | None = None) -> None:
        """Show the row containing ``offset`` (``rows_above`` rows above it; default a
        third of the page) unless it is already fully visible."""
        offset = max(0, min(int(offset), max(0, self.size - 1)))
        if rows_above is None:
            rows_above = self.page_rows() // 3
        self.moves += 1
        self._nav = ("show", offset, int(rows_above))
        self._resolve()

    def goto_top_offset(self, offset: int) -> None:
        """Make the row containing ``offset`` the first shown row."""
        self.moves += 1
        self._nav = ("top", max(0, min(int(offset), max(0, self.size - 1))))
        self._resolve()

    def goto_start(self) -> None:
        self.moves += 1
        self._nav = ("top", 0)
        self._resolve()

    def goto_end(self) -> None:
        self.moves += 1
        self._nav = ("end",)
        self._resolve()

    def scroll_rows(self, n: int) -> None:
        """Move the view by ``n`` rows (negative: up)."""
        if n == 0:
            return
        self.moves += 1
        nav = self._nav
        if nav is not None and nav[0] == "rows":
            n += nav[1]
        elif nav is not None:
            return          # a jump is waiting for data: wheel ticks are dropped meanwhile
        self._nav = ("rows", n)
        self._resolve()

    def _window(self, lo: int, hi: int) -> tuple[bytes, int] | None:
        got = self.store.window(lo, hi)
        if got is None:
            self._view.pump()
        return got

    def _end_top(self, page: int) -> int | None:
        if self._end is not None and self._end[0] == page:
            return self._end[1]
        rule = self._rule
        got = self._window(self.size - need_back(page, rule), self.size)
        if got is None:
            return None
        buf, base = got
        top = rows_back(buf, base, self.size, self.size, page, rule)
        self._end = (page, top)
        self._sync_vbar()
        return top

    def max_top(self) -> int | None:
        """The top of the last page (``None`` until the end of the file was read)."""
        page = self.page_rows()
        return self._end[1] if self._end is not None and self._end[0] == page else None

    def max_walk_rows(self) -> int:
        """Rows a single scroll step walks at most: its bytes fit in WALK_PAGES cached pages
        (a longer move continues step by step instead of asking for more pages than the
        cache keeps -- that never settled)."""
        rule = self._rule
        budget = WALK_PAGES * self.store.page - rule.lookback - rule.w - 64
        return max(1, budget // (rule.w + 4))

    def _apply_nav(self, nav: tuple, page: int) -> tuple[int, tuple | None] | None:
        """``(new top, navigation still pending)`` or ``None`` while pages are missing."""
        kind = nav[0]
        size = self.size
        rule = self._rule
        if kind == "end":
            top = self._end_top(page)
            return None if top is None else (top, None)
        if kind == "top":
            x = nav[1]
            got = self._window(x - rule.lookback - 8, x + 8)
            if got is None:
                return None
            return row_start_at(got[0], got[1], x, size, rule), None
        if kind == "show":
            x, above = nav[1], nav[2]
            rows = self._rows
            if len(rows) >= 2 and self._top == rows[0] and rows[0] <= x:
                full = min(len(rows) - 1, page)
                if full >= 1 and x < rows[full]:
                    return self._top, None        # already fully visible
            got = self._window(x - need_back(above, rule) - rule.w - 8, x + 8)
            if got is None:
                return None
            buf, base = got
            r = row_start_at(buf, base, x, size, rule)
            return rows_back(buf, base, r, size, above, rule), None
        if kind == "rows":
            n = nav[1]
            top = self._top
            step = min(abs(n), self.max_walk_rows())
            rest = n - step if n > 0 else n + step
            if n > 0:
                got = self._window(top - rule.lookback - 8, top + need_forward(step, rule))
                if got is None:
                    return None
                walked = walk_rows(got[0], got[1], top, size, step, rule)
                if walked[-1] >= size:            # the end of the file: nothing more to walk
                    return (walked[-2] if len(walked) >= 2 else top), None
                return walked[-1], (("rows", rest) if rest else None)
            got = self._window(top - need_back(step, rule), top + 8)
            if got is None:
                return None
            new = rows_back(got[0], got[1], top, size, step, rule)
            return new, (("rows", rest) if rest and new > 0 else None)
        return self._top, None

    def _resolve(self) -> None:
        """Apply the pending navigation and compute the shown rows from the cached pages
        (requesting what is missing; called again when pages arrive)."""
        if self.size <= 0:
            self._nav = None
            self._waiting = False
            self._rows = [0] if self.size == 0 else []
            self.viewport().update()
            return
        self._waiting = True
        if not self._view.io_allowed():
            return
        page = self.page_rows()
        if self._end is not None and self._end[0] != page:
            self._forget_end()                    # another page height (a scrollbar appeared, a resize)
        while self._nav is not None:
            got_nav = self._apply_nav(self._nav, page)
            if got_nav is None:
                return                            # waiting for pages (the navigation stays pending)
            top, rest = got_nav
            self._top = max(0, min(top, self.size - 1))
            self._nav = rest
        want = self._window_rows() + 1
        rule = self._rule
        got = self._window(self._top - rule.lookback - 8, self._top + need_forward(want, rule))
        if got is None:
            return
        buf, base = got
        rows = walk_rows(buf, base, self._top, self.size, want, rule)
        if len(rows) - 1 < page and self._top > 0:
            end_top = self._end_top(page)
            if end_top is None:
                return
            if end_top < self._top:
                self._top = end_top
                got = self._window(self._top - rule.lookback - 8, self._top + need_forward(want, rule))
                if got is None:
                    return
                buf, base = got
                rows = walk_rows(buf, base, self._top, self.size, want, rule)
        self._nav = None
        self._waiting = False
        self._rows = rows
        self._buf, self._base = buf, base
        self._decoded.clear()
        if self._line_anchor is None and base == 0:
            self._line_anchor = (0, 1)
            self._update_gutter()
        self._sync_vbar()
        self._sync_hbar()
        if self._end_wanted and self._end_top(page) is not None:
            self._end_wanted = False              # the scrollbar maps the same range as before
        self.store.prefetch(self._top - 2 * PAGE_BYTES, rows[-1] + 2 * PAGE_BYTES)
        self._view.pump()
        self.viewport().update()
        self.position_changed.emit()
        self.settled.emit()

    def _forget_end(self) -> None:
        """The last page's top is stale (other rows / page height): read it again once the
        shown rows are there, when it was known (the scrollbar range stays the same)."""
        if self._end is not None:
            self._end_wanted = True
        self._end = None

    def data_arrived(self) -> None:
        """New pages are cached: retry the navigation / rows (and a stale end)."""
        if self._waiting or self._nav is not None or len(self._rows) < 2 or self._end_wanted:
            self._resolve()

    def abandon_wait(self) -> None:
        """Reading failed: stop waiting for data (the placeholder says why)."""
        self._nav = None
        self._waiting = False
        self.store.urgent = []
        self.store.low = []
        self.viewport().update()

    # ------------------------------------------------------------------ scrollbars
    def _vmax(self) -> int:
        mt = self.max_top()
        return self.size if mt is None else mt

    def _top_to_value(self, top: int) -> int:
        m = self._vmax()
        if m <= _INT_MAX:
            return min(top, m)
        return min(_SCALE, (top * _SCALE + m // 2) // m)

    def _value_to_offset(self, value: int) -> int:
        m = self._vmax()
        if m <= _INT_MAX:
            return value
        return (value * m + _SCALE // 2) // _SCALE

    def _sync_vbar(self) -> None:
        bar = self.verticalScrollBar()
        m = self._vmax()
        shown = (self._rows[-1] - self._rows[0]) if len(self._rows) >= 2 else self._rule.w
        self._syncing = True
        try:
            if m <= _INT_MAX:
                bar.setRange(0, max(0, m))
                bar.setPageStep(max(1, shown))
                bar.setSingleStep(max(1, self._rule.w))
            else:
                bar.setRange(0, _SCALE)
                bar.setPageStep(max(1, shown * _SCALE // m))
                bar.setSingleStep(1)
            bar.setValue(self._top_to_value(self._top))
        finally:
            self._syncing = False

    def _on_vbar_action(self, action: int) -> None:
        a = QAbstractSlider.SliderAction
        page = self.page_rows()
        moves = {a.SliderSingleStepAdd.value: 1, a.SliderSingleStepSub.value: -1,
                 a.SliderPageStepAdd.value: max(1, page - 1), a.SliderPageStepSub.value: -max(1, page - 1)}
        act = int(action.value) if hasattr(action, "value") else int(action)
        bar = self.verticalScrollBar()
        if act in moves:
            bar.setSliderPosition(bar.value())     # no valueChanged: we move by rows
            self.scroll_rows(moves[act])
        elif act == a.SliderToMinimum.value:
            bar.setSliderPosition(bar.value())
            self.goto_start()
        elif act == a.SliderToMaximum.value:
            bar.setSliderPosition(bar.value())
            self.goto_end()

    def _on_vbar_value(self, value: int) -> None:
        if self._syncing:
            return
        self.moves += 1
        bar = self.verticalScrollBar()
        if value >= bar.maximum() and value > 0:
            self._nav = ("end",)
        else:
            self._nav = ("top", max(0, min(self._value_to_offset(value), max(0, self.size - 1))))
        self._resolve()

    def _sync_hbar(self) -> None:
        bar = self.horizontalScrollBar()
        vw = self.viewport().width()
        m = max(0, self._gutter_w + self._content_w + 2 * self._pad - vw)
        self._hoff = max(0, min(self._hoff, m))
        self._syncing = True
        try:
            bar.setRange(0, m)
            bar.setPageStep(max(1, vw - self._gutter_w))
            bar.setSingleStep(max(4, round(self._cw * 3)))
            bar.setValue(self._hoff)
        finally:
            self._syncing = False

    def _on_hbar_value(self, value: int) -> None:
        if self._syncing:
            return
        self._hoff = value
        self.viewport().update()

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802 (Qt name)
        """Scrolling is driven by the scrollbar slots."""

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        self._fit_rule()
        if self.size > 0 and len(self._rows) >= 2 and len(self._rows) - 1 < self._window_rows() + 1:
            if self._nav is None:
                self._nav = ("top", self._top)
        self._resolve()
        self._sync_hbar()

    # ------------------------------------------------------------------ rows / text
    def row_text(self, start: int, end: int) -> tuple[str, list[int] | None, str]:
        """``(display text, char->byte starts, decoded text)`` of the row ``[start, end)``
        (the trailing newline left out). Needs the row in the shown bytes."""
        key = (start, end)
        got = self._decoded.get(key)
        if got is not None:
            return got
        data = self._buf[start - self._base:end - self._base]
        if data.endswith(b"\n"):
            data = data[:-1]
        text, starts = decode_row(data)
        got = (display_text(text), starts, text)
        self._decoded[key] = got
        return got

    def row_texts(self) -> list[str]:
        """Decoded text of every shown row (tests / copy of the visible part)."""
        return [self.row_text(s, e)[2] for s, e in self.rows()]

    def _advance(self, disp: str, ci: int) -> float:
        if ci <= 0:
            return 0.0
        if disp.isascii():
            return ci * self._cw
        return self._fmf.horizontalAdvance(disp[:ci])

    @staticmethod
    def _char_of(starts: list[int] | None, rel: int, n: int) -> int:
        """Index of the character containing byte ``rel`` of the row."""
        if starts is None:
            return max(0, min(rel, n))
        return max(0, min(bisect.bisect_right(starts, rel) - 1, n))

    @staticmethod
    def _char_at_or_after(starts: list[int] | None, rel: int, n: int) -> int:
        if starts is None:
            return max(0, min(rel, n))
        return max(0, min(bisect.bisect_left(starts, rel), n))

    def offset_at(self, pos: QPoint) -> int:
        """The byte offset at a viewport position (a boundary between characters)."""
        rows = self._rows
        if len(rows) < 2:
            return -1
        i = pos.y() // self._row_h
        if i < 0:
            return rows[0]
        if i >= len(rows) - 1:
            return rows[-1]
        r, e = rows[i], rows[i + 1]
        disp, starts, _text = self.row_text(r, e)
        x = pos.x() - self._gutter_w - self._pad + self._hoff
        n = len(disp)
        if x <= 0:
            ci = 0
        elif disp.isascii():
            ci = min(n, int(x / self._cw + 0.5))
        else:
            lo, hi = 0, n
            while lo < hi:   # first boundary whose x is past the point (rounded)
                mid = (lo + hi) // 2
                left = self._fmf.horizontalAdvance(disp[:mid])
                right = self._fmf.horizontalAdvance(disp[:mid + 1])
                if (left + right) / 2 < x:
                    lo = mid + 1
                else:
                    hi = mid
            ci = lo
        rel = ci if starts is None else starts[ci]
        return r + rel

    # ------------------------------------------------------------------ painting
    def _palette_colors(self) -> _Colors:
        if self._colors is None:
            self._colors = _Colors(self.palette())
        return self._colors

    def _on_theme_changed(self) -> None:
        self._colors = None
        self.viewport().update()

    def changeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().changeEvent(event)
        if event.type() == event.Type.PaletteChange:
            self._colors = None

    def paintEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        p = QPainter(self.viewport())
        try:
            self._paint(p)
        finally:
            p.end()

    def _paint(self, p: QPainter) -> None:
        c = self._palette_colors()
        vp = self.viewport().rect()
        w, h = vp.width(), vp.height()
        p.fillRect(vp, c.base)
        p.setFont(self._font)
        gw = self._gutter_w
        rows = self._rows
        if self.size <= 0 or len(rows) < 2:
            p.setPen(c.muted)
            text = self._view.placeholder_text()
            p.drawText(QRect(gw, 0, max(0, w - gw), h), Qt.AlignmentFlag.AlignCenter, text)
            self._paint_gutter(p, c, h, [])
            return
        rh = self._row_h
        asc = self._fm.ascent() + 1
        tx0 = gw + self._pad - self._hoff
        sel = self.selection()
        spans: list[tuple[int, int, QColor]] = []
        if self._node_span is not None:
            spans.append((self._node_span[0], self._node_span[1], c.node))
        lo, hi = rows[0], rows[-1]
        if self._matches:
            k = max(0, bisect.bisect_left(self._matches, (lo, -1)) - 1)
            while k < len(self._matches) and self._matches[k][0] < hi:
                ms, me = self._matches[k]
                if me > lo:
                    spans.append((ms, me, c.current_match if k == self._match_cur else c.match))
                k += 1
        if sel is not None:
            spans.append((sel[0], sel[1], c.sel))
        p.setClipRect(QRect(gw, 0, max(0, w - gw), h))
        content_w = self._content_w
        shown: list[tuple[int, int, int]] = []
        for i in range(len(rows) - 1):
            y = i * rh
            if y >= h:
                break
            r, e = rows[i], rows[i + 1]
            shown.append((r, e, y))
            disp, starts, text = self.row_text(r, e)
            n = len(disp)
            nl = e - r - 1 if e > r and self._buf[e - 1 - self._base] == 0x0A else -1
            for s0, s1, color in spans:
                a, b = max(s0, r), min(s1, e)
                if a >= b:
                    continue
                x1 = self._advance(disp, self._char_of(starts, a - r, n))
                x2 = self._advance(disp, self._char_at_or_after(starts, b - r, n))
                if nl >= 0 and b - r > nl:
                    x2 += self._cw * 0.6          # the newline byte is selected too
                p.fillRect(QRectF(tx0 + x1, y, max(2.0, x2 - x1), rh), color)
            err = self._error_at
            if err is not None and r <= err < e:
                ci = self._char_of(starts, err - r, n)
                x1 = self._advance(disp, ci)
                x2 = self._advance(disp, ci + 1) if ci < n else x1 + self._cw
                p.fillRect(QRectF(tx0 + x1, y, max(self._cw * 0.6, x2 - x1), rh), c.error_bg)
                p.fillRect(QRectF(tx0 + x1, y + rh - 2, max(self._cw * 0.6, x2 - x1), 2), c.error)
            base_y = y + asc
            if _SPECIAL_RE.search(text) is None:
                p.setPen(c.text)
                p.drawText(QPointF(tx0, base_y), disp)
                width = self._advance(disp, n)
            else:
                x = 0.0
                pos = 0
                for m in _SPECIAL_RE.finditer(text):
                    a, b = m.span()
                    if a > pos:
                        seg = disp[pos:a]
                        p.setPen(c.text)
                        p.drawText(QPointF(tx0 + x, base_y), seg)
                        x += self._fmf.horizontalAdvance(seg)
                    seg = disp[a:b]
                    p.setPen(c.invalid if text[a] == "\ufffd" else c.faint)
                    p.drawText(QPointF(tx0 + x, base_y), seg)
                    x += self._fmf.horizontalAdvance(seg)
                    pos = b
                if pos < n:
                    seg = disp[pos:]
                    p.setPen(c.text)
                    p.drawText(QPointF(tx0 + x, base_y), seg)
                    x += self._fmf.horizontalAdvance(seg)
                width = x
            content_w = max(content_w, int(width) + 1)
        p.setClipping(False)
        self._paint_gutter(p, c, h, shown)
        if content_w > self._content_w:
            self._content_w = content_w
            QTimer.singleShot(0, self, self._sync_hbar)

    def _paint_gutter(self, p: QPainter, c: _Colors, h: int, shown: list[tuple[int, int, int]]) -> None:
        gw = self._gutter_w
        p.fillRect(QRect(0, 0, gw, h), c.gutter)
        p.setPen(c.line)
        p.drawLine(gw - 1, 0, gw - 1, h)
        rh = self._row_h
        lines = self._line_w > 0 and self.anchor_usable()
        for r, _e, y in shown:
            p.setPen(c.muted)
            p.drawText(QRect(0, y, self._off_w - self._pad, rh),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), self.offset_text(r))
            if lines and is_line_start(self._buf, self._base, r):
                line = self.line_of_row(r)
                if line is not None:
                    p.setPen(c.faint)
                    p.drawText(QRect(self._off_w, y, self._line_w - self._pad, rh),
                               int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), str(line))

    # ------------------------------------------------------------------ input
    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 (Qt name)
        delta = event.angleDelta()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if delta.y():
                self._view.zoom(1 if delta.y() > 0 else -1)
            event.accept()
            return
        if delta.y():
            self._wheel += -delta.y() / 120.0 * 3
            n = int(self._wheel)
            if n:
                self._wheel -= n
                self.scroll_rows(n)
        elif delta.x():
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - delta.x())
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt name)
        key = event.key()
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        page = max(1, self.page_rows() - 1)
        if event.matches(QKeySequence.StandardKey.Copy):
            self._view.copy_selection()
        elif key == Qt.Key.Key_Down:
            self.scroll_rows(1)
        elif key == Qt.Key.Key_Up:
            self.scroll_rows(-1)
        elif key == Qt.Key.Key_PageDown:
            self.scroll_rows(page)
        elif key == Qt.Key.Key_PageUp:
            self.scroll_rows(-page)
        elif key == Qt.Key.Key_Home and ctrl:
            self.goto_start()
        elif key == Qt.Key.Key_End and ctrl:
            self.goto_end()
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_End):
            bar = self.horizontalScrollBar()
            step = bar.singleStep()
            target = {Qt.Key.Key_Home: 0, Qt.Key.Key_End: bar.maximum(), Qt.Key.Key_Left: bar.value() - step,
                      Qt.Key.Key_Right: bar.value() + step}[key]
            bar.setValue(target)
        elif key == Qt.Key.Key_Escape:
            self.clear_selection()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        off = self.offset_at(event.position().toPoint())
        if off < 0:
            return
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and self._anchor >= 0:
            self._cursor = off
        else:
            self._anchor = self._cursor = off
        self._drag = True
        self.viewport().update()
        self.selection_changed.emit()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        if not self._drag:
            return
        pos = event.position().toPoint()
        self._drag_pos = pos
        h = self.viewport().height()
        if pos.y() < 0 or pos.y() >= h:
            if not self._auto.isActive():
                self._auto.start()
            return
        self._auto.stop()
        off = self.offset_at(pos)
        if off >= 0 and off != self._cursor:
            self._cursor = off
            self.viewport().update()
            self.selection_changed.emit()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        self._drag = False
        self._auto.stop()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        """Select the row under the mouse."""
        rows = self._rows
        i = event.position().toPoint().y() // self._row_h
        if 0 <= i < len(rows) - 1:
            self.set_selection(rows[i], rows[i + 1])

    def _auto_scroll(self) -> None:
        if not self._drag:
            self._auto.stop()
            return
        y = self._drag_pos.y()
        self.scroll_rows(-1 if y < 0 else 1)
        clamped = QPoint(self._drag_pos.x(), max(0, min(y, self.viewport().height() - 1)))
        off = self.offset_at(clamped)
        if off >= 0:
            self._cursor = off
            self.viewport().update()
            self.selection_changed.emit()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802 (Qt name)
        off = self.offset_at(self.viewport().mapFromGlobal(event.globalPos()))
        menu = self._view.build_context_menu(off)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(event.globalPos())


# ==================================================================================
# the view
# ==================================================================================
class RawView(ViewBase):
    """Raw bytes of the data file (see the module docstring)."""

    view_id = "raw"
    #: Emitted whenever the requested position is shown with data (tests / status).
    settled = Signal()
    #: The selected byte span changed (status bar / search prefill).
    selection_changed = Signal()

    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(session, parent)
        self.setObjectName("RawView")
        self._doc: Any = None
        self.line_index: LineIndex | None = None
        self._fetcher = PageFetcher(self)
        self._fetcher.arrived.connect(self._pages_arrived)
        self._gen = 0
        self._dead = False
        self._pending_ref: Any = None
        self._ref_dirty = True
        self._node_task: Any = None
        self._anchor_task: Any = None
        self._index_task: Any = None
        self._pending_line: int | None = None
        self._find_region: tuple[int, int] | None = None
        self._find_key: tuple[str, bool] | None = None
        self._copy_task: Any = None
        self._read_error: BaseException | None = None
        self._find_whole = False
        self._counting_shown = False
        self._follow_jump = True              # the next resolved current node is scrolled into view
        self._edited_note = False             # the info bar shows the "edited node" note

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        bar = QHBoxLayout()
        bar.setContentsMargins(4, 2, 4, 0)
        bar.setSpacing(6)
        self.offset_edit = QLineEdit(self)
        self.offset_edit.setObjectName("raw_offset")
        self.offset_edit.setPlaceholderText(t("gui.raw.offset.placeholder"))
        self.offset_edit.setToolTip(t("gui.raw.offset.tip"))
        self.offset_edit.setClearButtonEnabled(True)
        self.offset_edit.setMaximumWidth(180)
        self.offset_edit.returnPressed.connect(self._offset_entered)
        self.line_edit = QLineEdit(self)
        self.line_edit.setObjectName("raw_line")
        self.line_edit.setPlaceholderText(t("gui.raw.line.placeholder"))
        self.line_edit.setToolTip(t("gui.raw.line.tip"))
        self.line_edit.setClearButtonEnabled(True)
        self.line_edit.setMaximumWidth(140)
        self.line_edit.returnPressed.connect(self._line_entered)
        bar.addWidget(QLabel(t("gui.raw.offset.label"), self))
        bar.addWidget(self.offset_edit)
        bar.addWidget(QLabel(t("gui.raw.line.label"), self))
        bar.addWidget(self.line_edit)

        self.hex_button = QToolButton(self)
        self.hex_button.setObjectName("raw_hex")
        self.hex_button.setText(t("gui.raw.hex"))
        self.hex_button.setToolTip(t("gui.raw.hex.tip"))
        self.hex_button.setCheckable(True)
        self.hex_button.toggled.connect(self.set_hex)
        bar.addWidget(self.hex_button)
        self.lines_button = QToolButton(self)
        self.lines_button.setObjectName("raw_lines")
        self.lines_button.setText(t("gui.raw.lines"))
        self.lines_button.setToolTip(t("gui.raw.lines.tip"))
        self.lines_button.clicked.connect(lambda _c=False: self.build_line_index())
        bar.addWidget(self.lines_button)
        self.error_button = QToolButton(self)
        self.error_button.setObjectName("raw_goto_error")
        self.error_button.setText(t("gui.raw.goto_error"))
        self.error_button.setToolTip(t("gui.raw.goto_error.tip"))
        self.error_button.clicked.connect(lambda _c=False: self.goto_error())
        self.error_button.setVisible(False)
        bar.addWidget(self.error_button)
        bar.addStretch(1)
        self.prepared_label = QLabel(t("gui.raw.prepared"), self)
        self.prepared_label.setObjectName("raw_prepared")
        self.prepared_label.setEnabled(False)       # muted
        self.prepared_label.setVisible(False)
        bar.addWidget(self.prepared_label)
        self.position_label = QLabel("", self)
        self.position_label.setObjectName("raw_position")
        self.position_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        bar.addWidget(self.position_label)
        layout.addLayout(bar)

        self.info_bar = InfoBar("info", "", parent=self)
        self.info_bar.setVisible(False)
        layout.addWidget(self.info_bar)

        self.area = RawArea(self)
        layout.addWidget(self.area, 1)
        self.find_bar = QuickFindBar(self)
        layout.addWidget(self.find_bar)
        self.find_bar.search_changed.connect(self._on_find_changed)
        self.find_bar.next_requested.connect(lambda: self.find_next(False))
        self.find_bar.prev_requested.connect(lambda: self.find_next(True))
        self.find_bar.closed.connect(lambda: self.area.setFocus())

        self.area.position_changed.connect(self._update_position)
        self.area.selection_changed.connect(self.selection_changed)
        self.area.settled.connect(self._on_area_settled)

        self._index_timer = QTimer(self)
        self._index_timer.setInterval(INDEX_POLL_MS)
        self._index_timer.timeout.connect(self._poll_index)

        session.current_changed.connect(self._on_current_changed)
        session.opened.connect(self._sync_doc)
        session.view_changed.connect(self._sync_doc)       # reopen: the document is replaced
        session.index_finished.connect(self._on_index_finished)
        session.state_changed.connect(self._on_state_changed)
        data_changed = getattr(session, "data_changed", None)
        if data_changed is not None:
            data_changed.connect(self._on_data_changed)
        settings_changed = getattr(session.app, "settings_changed", None)
        if settings_changed is not None:
            settings_changed.connect(self._on_settings_changed)
        self._sync_doc()

    # ------------------------------------------------------------------ document
    def io_allowed(self) -> bool:
        """Hidden views do no I/O."""
        return self.isVisible() and self._doc is not None

    def _sync_doc(self) -> None:
        doc = getattr(self.session, "doc", None)
        if doc is self._doc:
            return
        self._gen += 1
        self._fetcher.drop_queued()
        # tasks of the old document: their results are dropped (generation); the line
        # count of the old file stops, so go-to-line works on the new one at once
        for name in ("_index_task", "_anchor_task", "_node_task", "_copy_task"):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                setattr(self, name, None)
        self._doc = doc
        self._read_error = None
        self.line_index = line_index_for(doc) if doc is not None else None
        self.area.reset(int(doc.data_size) if doc is not None else 0)
        self._find_region = None
        self._ref_dirty = True
        self._pending_ref = getattr(self.session, "current", None)
        converted = bool(doc is not None and getattr(getattr(doc, "prepared", None), "converted", False))
        self.prepared_label.setVisible(converted)
        if converted:
            info = getattr(doc, "info", None)
            self.prepared_label.setToolTip(t("gui.raw.prepared.tip", encoding=getattr(info, "encoding", "?"),
                                             compression=getattr(info, "compression", None) or "-"))
        self._refresh_error()
        self._refresh_lines_button()
        self.info_bar.setVisible(False)
        self._edited_note = False
        self._update_position()
        if self.isVisible():
            self._resume()

    def _on_state_changed(self, state: str) -> None:
        if state == "closed":
            self._gen += 1          # late results of the closed document are dropped
            self._index_timer.stop()
        elif state in ("indexing", "ready"):
            self._sync_doc()

    def _on_index_finished(self) -> None:
        self._refresh_error()

    def _refresh_error(self) -> None:
        doc = self._doc
        offset = getattr(doc, "error_data_offset", None) if doc is not None and doc.error is not None else None
        self.area.set_error_offset(offset)
        self.error_button.setVisible(offset is not None)
        if offset is not None and doc is not None:
            self.error_button.setToolTip(t("gui.raw.goto_error.tip") + "\n" + doc.error.message)

    def placeholder_text(self) -> str:
        if self._doc is None:
            return t("gui.raw.no_doc")
        if self.area.size == 0:
            return t("gui.raw.empty")
        if self._read_error is not None:
            return t("gui.raw.read_failed.short")
        return t("gui.common.loading")

    # ------------------------------------------------------------------ activation
    def activate(self) -> None:
        self._resume()

    def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        QTimer.singleShot(0, self, self._resume)

    def _resume(self) -> None:
        """The view is visible: load what is pending."""
        if not self.isVisible():
            return
        if self.session.doc is not self._doc:
            self._sync_doc()
            return
        if self._doc is None:
            return
        if self._ref_dirty:
            self._resolve_node()
        self.area._resolve()
        self._maybe_auto_index()
        if self._pending_line is not None:
            self._poll_index()

    # ------------------------------------------------------------------ reading
    def pump(self) -> None:
        """Hand the wanted pages to the reader thread: one read runs, the newest wishes
        wait behind it (an older wish not started yet is replaced)."""
        if not self.io_allowed() or self._dead:
            return
        fetcher = self._fetcher
        if not fetcher.idle() and not fetcher.queued() and not self.area.store.urgent:
            return                     # a read runs; low-priority pages can wait for it
        pages = self.area.store.take(READ_BATCH)
        if not pages:
            return
        fetcher.submit(self._gen, self._doc, pages)

    def _pages_arrived(self, gen: int, result: dict[int, bytes] | None, error: BaseException | None) -> None:
        if gen != self._gen or self._dead:
            self.pump()
            return
        if error is not None:
            self._read_failed(gen, error)
            return
        self.area.store.put(result or {})
        self.area.data_arrived()
        self.pump()

    def _read_failed(self, gen: int, exc: BaseException) -> None:
        if gen != self._gen:
            return
        self._read_error = exc
        self.area.abandon_wait()
        message = exc.message if isinstance(exc, JsonReshapeError) else str(exc)
        self.show_message("error", t("gui.raw.read_failed", message=message))
        log.info("raw view read failed: %r", exc)

    def is_settled(self) -> bool:
        """Nothing is waiting for data (tests)."""
        return self.area.is_settled() and self._fetcher.idle()

    def _on_area_settled(self) -> None:
        if self._anchor_wanted():
            self._request_anchor()
        self.settled.emit()

    # ------------------------------------------------------------------ messages
    def show_message(self, kind: str, text: str, buttons: list[tuple[str, Callable[[], Any]]] | None = None) -> None:
        bar = self.info_bar
        bar.set_kind(kind)
        bar.set_message(text)
        bar.set_detail(None)
        bar.clear_buttons()
        for label, callback in buttons or []:
            bar.add_button(label, callback)
        bar.setVisible(True)

    def message(self) -> str | None:
        """The text of the visible info bar (tests)."""
        return self.info_bar.message() if self.info_bar.isVisibleTo(self) else None

    # ------------------------------------------------------------------ navigation API
    def goto_offset(self, offset: int, length: int = 1) -> None:
        """Show byte ``offset`` of the data file and select ``[offset, offset + length)``
        (clamped to the file). Works before the view is visible (applied when shown)."""
        offset = int(offset)
        length = max(0, int(length))
        size = self.area.size
        if self._doc is not self.session.doc:
            self._sync_doc()
            size = self.area.size
        offset = max(0, min(offset, size))
        self._follow_jump = False             # an explicit target wins over following the node
        self.area.set_selection(offset, min(size, offset + length))
        if size <= 0:
            return
        self.area.goto(min(offset, size - 1))

    def goto_line(self, line: int) -> None:
        """Show the start of line ``line`` (1-based). Counts the lines first when needed
        (a task with progress; the GUI never waits)."""
        line = int(line)
        if line < 1:
            raise ValueError("line numbers start at 1")
        if self._doc is not self.session.doc:
            self._sync_doc()
        idx = self.line_index
        if idx is None:
            return
        self._follow_jump = False
        self._pending_line = line
        self._poll_index()

    def goto_error(self) -> None:
        offset = self.area.error_offset()
        if offset is not None:
            self.goto_offset(offset, 1)

    def set_hex(self, on: bool) -> None:
        self.area.set_hex(on)
        if self.hex_button.isChecked() != bool(on):
            self.hex_button.setChecked(bool(on))
        self._update_position()

    def _offset_entered(self) -> None:
        text = self.offset_edit.text()
        try:
            offset = parse_offset(text)
        except ValueError:
            self.show_message("warning", t("gui.raw.bad_offset", text=text))
            return
        size = self.area.size
        if offset >= size > 0:
            self.show_message("info", t("gui.raw.offset_past_end", offset=format_count(offset),
                                        size=format_count(size)))
            offset = size - 1
        else:
            self.info_bar.setVisible(False)
        self.goto_offset(offset, 1)
        self.area.setFocus()

    def _line_entered(self) -> None:
        text = self.line_edit.text()
        try:
            line = parse_line(text)
        except ValueError:
            self.show_message("warning", t("gui.raw.bad_line", text=text))
            return
        self.info_bar.setVisible(False)
        self.goto_line(line)
        self.area.setFocus()

    # ------------------------------------------------------------------ line index
    def _maybe_auto_index(self) -> None:
        idx = self.line_index
        if idx is None or idx.complete or idx.building or self._index_task is not None:
            return
        if self.area.size <= AUTO_LINE_INDEX_BYTES:
            self.build_line_index(silent=True)

    def build_line_index(self, *, silent: bool = False) -> Any:
        """Count the lines of the data file (task kind ``lineindex``, progress in bytes,
        cancellable). Returns the task (``None`` when nothing to do)."""
        idx = self.line_index
        doc = self._doc
        if idx is None or doc is None or idx.complete or self._index_task is not None:
            return None
        if not idx.try_begin():
            self._index_timer.start()     # another view builds it: follow its progress
            return None
        gen = self._gen

        def work(ctx: Any) -> None:
            with doc.open_data(buffering=0) as f:
                idx.build(f, progress=ctx.progress, cancel=ctx.cancel, reserved=True)

        def finished(_result: Any = None) -> None:
            if gen == self._gen:
                self._index_task = None
                self._poll_index()
                self._refresh_lines_button()

        def failed(exc: BaseException) -> None:
            if gen == self._gen:
                self._index_task = None
                self._refresh_lines_button()
                if self._pending_line is not None:
                    self._pending_line = None
                    message = exc.message if isinstance(exc, JsonReshapeError) else str(exc)
                    self.show_message("error", t("gui.raw.read_failed", message=message))

        def cancelled() -> None:
            if gen == self._gen:
                self._index_task = None
                self._pending_line = None
                self._refresh_lines_button()

        try:
            self._index_task = self.session.app.tasks.run(
                work, title=t("gui.raw.task.lineindex", name=getattr(self.session, "name", "")), kind="lineindex",
                owner=self, silent=silent, on_done=finished, on_error=failed, on_cancel=cancelled)
        except BaseException:
            idx.end()
            raise
        self._index_timer.start()
        self._refresh_lines_button()
        return self._index_task

    def _refresh_lines_button(self) -> None:
        idx = self.line_index
        show = idx is not None and not idx.complete
        self.lines_button.setVisible(bool(show))
        self.lines_button.setEnabled(bool(show) and self._index_task is None and not (idx and idx.building))

    def _poll_index(self) -> None:
        """Follow the line index: resolve a pending go-to-line, refresh line numbers."""
        idx = self.line_index
        if idx is None:
            self._index_timer.stop()
            return
        line = self._pending_line
        if line is not None:
            if idx.covers_line(line):
                self._pending_line = None
                self._start_line_lookup(line)
            elif idx.complete:
                self._pending_line = None
                self._hide_counting()
                self.show_message("warning", t("gui.raw.line_past_end", line=format_count(line),
                                               count=format_count(idx.line_count or 0)))
            else:
                if not idx.building and self._index_task is None and self.io_allowed():
                    self.build_line_index()
                self._counting_shown = True
                self.show_message("info", t("gui.raw.counting", line=format_count(line),
                                            percent=int(idx.fraction() * 100)))
        if self._pending_line is None:
            self._hide_counting()
        if self._anchor_wanted():
            self._request_anchor()
        if idx.complete or (not idx.building and self._pending_line is None):
            self._index_timer.stop()
            self._refresh_lines_button()
            self._update_gutter_lines()
        elif not self._index_timer.isActive():
            self._index_timer.start()

    def _hide_counting(self) -> None:
        if self._counting_shown:
            self._counting_shown = False
            if self.info_bar.kind() == "info":
                self.info_bar.setVisible(False)

    def _update_gutter_lines(self) -> None:
        self.area._update_gutter()
        self.area.viewport().update()
        self._update_position()

    def _start_line_lookup(self, line: int) -> None:
        idx, doc, gen = self.line_index, self._doc, self._gen
        if idx is None or doc is None:
            return

        def work(ctx: Any) -> int:
            with doc.open_data(buffering=0) as f:
                return idx.line_start(line, f)

        def done(offset: int) -> None:
            if gen != self._gen:
                return
            self._hide_counting()
            self.area.set_line_anchor(offset, line)
            size = self.area.size
            self.goto_offset(offset, 1 if offset < size else 0)

        def failed(exc: BaseException) -> None:
            if gen == self._gen:
                message = exc.message if isinstance(exc, JsonReshapeError) else str(exc)
                self.show_message("error", t("gui.raw.read_failed", message=message))

        self.session.app.tasks.run(work, title=t("gui.raw.task.read"), kind="raw", owner=self, silent=True,
                                   on_done=done, on_error=failed)

    def _anchor_wanted(self) -> bool:
        idx = self.line_index
        if idx is None or self._anchor_task is not None or not self.area.is_settled() or self.area.size <= 0:
            return False
        if self.area.anchor_usable():
            return False
        return idx.covers_offset(self.area.top())

    def _request_anchor(self) -> None:
        idx, doc, gen = self.line_index, self._doc, self._gen
        if idx is None or doc is None or not self.io_allowed():
            return
        top = self.area.top()

        def work(ctx: Any) -> int:
            with doc.open_data(buffering=0) as f:
                return idx.line_of(top, f)

        def done(line: int) -> None:
            self._anchor_task = None
            if gen == self._gen:
                self.area.set_line_anchor(top, line)

        def failed(exc: BaseException) -> None:
            self._anchor_task = None
            log.debug("line lookup failed: %r", exc)

        self._anchor_task = self.session.app.tasks.run(work, title=t("gui.raw.task.read"), kind="raw", owner=self,
                                                       silent=True, on_done=done, on_error=failed,
                                                       on_cancel=lambda: setattr(self, "_anchor_task", None))

    # ------------------------------------------------------------------ current node
    def _on_current_changed(self, ref: Any, origin: Any) -> None:
        if origin is self:
            return
        self._pending_ref = ref
        self._ref_dirty = True
        self._follow_jump = True
        if self.isVisible():
            self._resolve_node()

    def _on_data_changed(self, cs: Any) -> None:
        """Edits were applied: the current node (remapped by the session) may now lie in the
        edit layer, or back in the file after an undo -- look it up again, without jumping."""
        if self._dead:
            return
        self._pending_ref = getattr(self.session, "current", None)
        self._ref_dirty = True
        self._follow_jump = False
        if self.isVisible():
            self._resolve_node()

    def _set_edited_note(self, on: bool) -> None:
        """Show / hide the note for a current node that lies in the edit layer."""
        if on:
            self._edited_note = True
            self.show_message("info", t("gui.raw.edited_note"))
        elif self._edited_note:
            self._edited_note = False
            if self.info_bar.message() == t("gui.raw.edited_note"):
                self.info_bar.setVisible(False)

    def edited_note_shown(self) -> bool:
        """The "edited node" note is shown (tests)."""
        return self._edited_note and self.info_bar.isVisibleTo(self)

    def _resolve_node(self) -> None:
        self._ref_dirty = False
        ref = self._pending_ref
        if self._node_task is not None:
            self._node_task.cancel()
            self._node_task = None
        if ref is None or self._doc is None:
            self.area.set_node_span(None)
            self._set_edited_note(False)
            return
        session, gen = self.session, self._gen
        moves = self.area.moves
        # the state ``ref`` belongs to, pinned NOW: a ChangeSet applied before the worker starts must not make it
        # resolve the ref in another state (review CL-9; None while indexing: the live document)
        doc = task_doc(session)

        def work(ctx: Any) -> tuple[int, int] | None:
            node = task_node(session, ref, ctx.cancel, doc=doc)
            if getattr(node, "is_virtual", False) or not tuple(getattr(node, "path", ())):
                return None       # the document root: tinting the whole file says nothing
            return int(node.start), int(node.end)

        def done(span: tuple[int, int] | None) -> None:
            if gen != self._gen or self._pending_ref is not ref:
                return
            self._node_task = None
            edited = span is not None and edited_span(span[0])
            self._set_edited_note(edited)
            if edited:
                span = None       # no bytes in the data file: nothing to tint or jump to
            self.area.set_node_span(span)
            # jump to the node unless the user (or an explicit goto) moved meanwhile
            if span is not None and self.isVisible() and self._follow_jump and self.area.moves == moves:
                self.area.goto(span[0])

        def failed(exc: BaseException) -> None:
            self._node_task = None
            log.debug("raw view: node %r not resolved: %r", ref, exc)
            if gen == self._gen:
                self.area.set_node_span(None)
                self._set_edited_note(False)

        self._node_task = session.app.tasks.run(work, title=t("gui.raw.task.node"), kind="raw", owner=self,
                                                silent=True, on_done=done, on_error=failed,
                                                on_cancel=lambda: None)

    # ------------------------------------------------------------------ position label
    def _update_position(self) -> None:
        size = self.area.size
        if self._doc is None or size <= 0:
            self.position_label.setText("")
            return
        top = self.area.top()
        pct = int(top * 100 / size) if size else 0
        text = t("gui.raw.position", offset=format_count(top), size=format_count(size), percent=pct)
        if self.area.is_settled() and self.area.anchor_usable():
            line = self.area.line_of_row(top)
            if line is not None:
                text += " · " + t("gui.raw.position.line", line=format_count(line))
        self.position_label.setText(text)

    # ------------------------------------------------------------------ copy
    def selected_text(self, limit: int = SELECTED_TEXT_MAX) -> str:
        """The selected bytes decoded as UTF-8 when they are loaded and at most ``limit``
        bytes long, else "" (prefills searches; never reads the file)."""
        sel = self.area.selection()
        if sel is None or sel[1] - sel[0] > limit or not self.area.store.has(*sel):
            return ""
        got = self.area.store.window(*sel)
        return got[0].decode("utf-8", errors="replace") if got is not None else ""

    def copy_selection(self) -> Any:
        """Copy the selected bytes (decoded as UTF-8, at most :data:`COPY_CAP` bytes).
        Returns the copied text, or the task reading the bytes, or ``None``."""
        sel = self.area.selection()
        if sel is None:
            return None
        a, b = sel
        capped = b - a > COPY_CAP
        if capped:
            b = a + COPY_CAP
        got = self.area.store.window(a, b) if self.area.store.has(a, b) else None
        if got is not None:
            return self._copy_done(got[0], capped)
        doc = self._doc
        if doc is None:
            return None

        def work(ctx: Any) -> bytes:
            return read_range(doc, a, b)

        self._copy_task = self.session.app.tasks.run(
            work, title=t("gui.raw.task.copy"), kind="raw", owner=self, silent=True,
            on_done=lambda data: self._copy_done(data, capped),
            on_error=lambda exc: self.show_message("error", t("gui.raw.read_failed", message=getattr(
                exc, "message", str(exc)))))
        return self._copy_task

    def _copy_done(self, data: bytes, capped: bool) -> str:
        self._copy_task = None
        if capped:   # a character cut by the cap is left out
            decoded = codecs.getincrementaldecoder("utf-8")(errors="replace").decode(data, final=False)
        else:
            decoded = data.decode("utf-8", errors="replace")
        text = set_clipboard_text(decoded)
        if capped:
            self.show_message("info", t("gui.raw.copy_capped", size=format_size(COPY_CAP)))
        return text

    def copy_value(self, fmt: str = "json") -> Any:
        """Ctrl+C: the selected bytes when there is a selection, else the current node."""
        if self.area.selection() is not None:
            return self.copy_selection()
        return super().copy_value(fmt)

    def build_context_menu(self, offset: int) -> QMenu:
        menu = QMenu(self)
        copy = QAction(t("gui.raw.menu.copy"), menu)
        copy.setObjectName("raw_copy")
        copy.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))   # shown only: the window owns Ctrl+C
        copy.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        copy.setShortcutVisibleInContextMenu(True)
        copy.setEnabled(self.area.selection() is not None)
        copy.triggered.connect(lambda _c=False: self.copy_selection())
        menu.addAction(copy)
        if offset >= 0:
            text = self.area.offset_text(offset) if self.area.hex_offsets() else str(offset)
            copy_off = QAction(t("gui.raw.menu.copy_offset", offset=text), menu)
            copy_off.setObjectName("raw_copy_offset")
            copy_off.triggered.connect(lambda _c=False, s=str(offset): set_clipboard_text(s))
            menu.addAction(copy_off)
        menu.addSeparator()
        hex_action = QAction(t("gui.raw.hex.tip"), menu)
        hex_action.setCheckable(True)
        hex_action.setChecked(self.area.hex_offsets())
        hex_action.toggled.connect(self.set_hex)
        menu.addAction(hex_action)
        return menu

    # ------------------------------------------------------------------ quick find
    def quick_find(self) -> None:
        text = None
        sel = self.area.selection()
        if sel is not None and sel[1] - sel[0] <= 200 and self.area.store.has(*sel):
            got = self.area.store.window(*sel)
            if got is not None:
                candidate = got[0].decode("utf-8", errors="replace")
                if "\n" not in candidate:
                    text = candidate
        self.find_bar.open_bar(text)

    def _on_find_changed(self, text: str, case: bool) -> None:
        if not text:
            self._find_key = None
            self._find_region = None
            self.area.set_matches([], None)
            return
        self._find_key = (text, case)
        self._run_find(keep_position=False)

    def _run_find(self, *, keep_position: bool) -> None:
        key = self._find_key
        if key is None:
            return
        matcher = self.find_bar.matcher()
        region = self.area.store.region_around(self.area.top())
        if matcher is None or region is None:
            self._find_region = None
            self.area.set_matches([], None)
            self.find_bar.set_status(status_text(None, 0), "notfound")
            return
        start, data = region
        self._find_region = (start, start + len(data))
        text = data.decode("utf-8", errors="surrogateescape")
        spans = matcher(text)
        matches: list[tuple[int, int]] = []
        ci = 0
        bi = start
        for s, e in spans:
            bi += len(text[ci:s].encode("utf-8", errors="surrogateescape"))
            be = bi + len(text[s:e].encode("utf-8", errors="surrogateescape"))
            matches.append((bi, be))
            ci = e
            bi = be
            if len(matches) >= FIND_MAX_MATCHES:
                break
        whole = start == 0 and start + len(data) >= self.area.size
        self._find_whole = whole and len(matches) < FIND_MAX_MATCHES
        if not matches:
            self.area.set_matches([], None)
            self.find_bar.set_status(status_text(None, 0, capped=not whole), "notfound")
            return
        top = self.area.top()
        cur = bisect.bisect_left(matches, (top, -1))
        if cur >= len(matches):
            cur = 0
        self.area.set_matches(matches, None if keep_position else cur)
        if not keep_position:
            self._select_match(cur)
        else:
            self.find_bar.set_status(status_text(None, len(matches), capped=not self._find_whole), "found")

    def _select_match(self, i: int) -> None:
        matches = self.area.matches()
        if not matches:
            return
        i %= len(matches)
        self.area.set_current_match(i)
        s, e = matches[i]
        self.area.set_selection(s, e)
        self.area.goto(s)
        self.find_bar.set_status(status_text(i, len(matches), capped=not getattr(self, "_find_whole", False)),
                                 "found")

    def find_next(self, backwards: bool = False) -> None:
        if self._find_key is None:
            if self.find_bar.text():
                self._find_key = (self.find_bar.text(), self.find_bar.case_sensitive())
            else:
                self.quick_find()
                return
        region = self.area.store.region_around(self.area.top())
        current = (region[0], region[0] + len(region[1])) if region is not None else None
        if current != self._find_region:
            self._run_find(keep_position=True)
        matches = self.area.matches()
        if not matches:
            return
        cur = self.area._match_cur
        if cur is None:
            pos = self.area.cursor_offset() if self.area.cursor_offset() >= 0 else self.area.top()
            if backwards:
                cur = bisect.bisect_left(matches, (pos, -1)) - 1
            else:
                cur = bisect.bisect_left(matches, (pos, -1))
        else:
            cur += -1 if backwards else 1
        self._select_match(cur)

    # ------------------------------------------------------------------ ViewBase
    def zoom(self, steps: int) -> None:
        self.area.zoom(steps)

    def _on_settings_changed(self) -> None:
        """The data font may have changed: take it, keeping the zoom steps."""
        if not self._dead:
            self.area.set_font(mono_font(self.session), keep_zoom=True)

    def shutdown(self) -> None:
        """Tab closing: stop the timers, the reader and detach the tasks (a running line
        count is cancelled; its thread releases the index when it ends)."""
        if self._dead:
            return
        self._dead = True
        self._index_timer.stop()
        self._gen += 1
        self._index_task = None
        self._fetcher.close()
        settings_changed = getattr(self.session.app, "settings_changed", None)
        if settings_changed is not None:
            try:
                settings_changed.disconnect(self._on_settings_changed)
            except (RuntimeError, TypeError):
                pass
        data_changed = getattr(self.session, "data_changed", None)
        if data_changed is not None:
            try:
                data_changed.disconnect(self._on_data_changed)
            except (RuntimeError, TypeError):
                pass
        super().shutdown()
