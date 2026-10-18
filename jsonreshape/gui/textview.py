"""TextView: pretty JSON of the current node (docs/ARCHITECTURE.md §11.7 "Text";
AGENT_PROMPT §4.3 first bullet, §4.6 linking).

The text is built on a worker thread from the node's **raw bytes** with the token-level
:class:`jsonreshape.core.Reformatter` (numbers and strings stay byte-exact: ``1e400``,
``1.10``, ``-0.0``, no ``\\u`` escaping), at most :data:`TEXT_CAP` bytes of output
(:func:`build_pretty`). The worker also computes the line starts and the fold regions
(:func:`scan_folds`: per opening line its closing line, depth, parent region, container
kind and the line spans of the preceding siblings) -- enough to find the node of any line
(:func:`node_path_at`) without a per-line table. The last :data:`PRETTY_CACHE` results are
kept, so switching back to a node does not format it again.

The editor (:class:`CodeEditor`, a read-only ``QPlainTextEdit``) works with *source lines*
(the lines of the pretty text) while its ``QTextDocument`` holds only the lines that are
not folded away (:class:`LineRanges` maps document blocks <-> source lines):

* the text is inserted in chunks of at most :data:`CHUNK_CHARS` characters, a bounded step
  per event-loop turn (never all at once: an indent change, a restored position, folding
  while loading and quick-find targets beyond the loaded part all wait for the steps);
* folding removes the folded lines from the document and unfolding inserts them again,
  in bounded slices (:data:`SLICE_LINES` lines / :data:`CHUNK_CHARS` characters per step);
  bulk changes (fold all, fold to level N, unfold all) reload the shown lines. Qt never
  sees hidden blocks, so nothing walks or lays out hundreds of thousands of invisible
  lines, and folding a big region frees its memory;
* syntax colors are applied lazily, per block, only to the blocks being painted (no
  ``QSyntaxHighlighter`` pass over the whole document on the GUI thread);
* characters a ``QTextDocument`` would turn into block boundaries (CR, U+2029, U+FDD0,
  U+FDD1) and other invisible ones are shown as visible symbols, one for one, so document
  blocks and source lines stay in step;
* lines longer than :data:`MAX_LINE_CHARS` characters are shortened (``…``) for display
  -- Qt's text layout becomes quadratic on very long lines, especially when wrapping;
  copying maps the selection to the complete text (a selection ending in a shortened
  tail takes the whole line, selecting everything copies the complete text);
* markers ▸ / ▾ in the gutter, fold all / unfold all / fold to level N.

Quick find (Ctrl+F) searches the shown text (:func:`~jsonreshape.gui.quickfind.make_matcher`;
large texts in a silent task), highlights the matches in the visible area, F3 / Shift+F3 move.

The view follows ``session.current_changed`` (only while visible -- :meth:`TextView.activate`
loads the pending node). Moving the caret with the keyboard or the mouse selects the node
at the caret in the other views (``session.select(ref, origin=self)``, debounced by
:data:`CARET_DEBOUNCE_MS`); the ref carries the child indices (``ipath``), so the exact
member is meant also among duplicate keys. Nodes too large for the text (or the virtual root of a JSON
Lines file) show an info bar whose button emits :attr:`TextView.open_raw_requested`
``(offset, length)``; a JSON error inside the node shows the valid part plus the error.
The data font follows the settings (``app.settings_changed``), keeping the zoom.
"""

from __future__ import annotations

import bisect
import codecs
import json
import logging
import re
import time
import weakref
from array import array
from dataclasses import dataclass, field
from itertools import accumulate, compress, islice, repeat
from operator import add, and_, eq, ge, gt, le, lt, not_, sub
from typing import Any, Iterator, NamedTuple

from PySide6.QtCore import QMimeData, QPoint, QRect, QRectF, QSize, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QContextMenuEvent,
    QFont,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QTextBlock,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextLayout,
    QTextOption,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextDocumentLayout,
    QPlainTextEdit,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jsonreshape.errors import Cancelled, FileChangedError, JsonReshapeError
from jsonreshape.gui import theme
from jsonreshape.gui.quickfind import QuickFindBar, status_text
from jsonreshape.gui.widgets import (
    ElidedLabel,
    InfoBar,
    ViewBase,
    format_count,
    format_size,
    mono_font,
    set_clipboard_text,
)
from jsonreshape.i18n import t
from jsonreshape.tasks import CancelToken, check

log = logging.getLogger(__name__)

__all__ = [
    "CHUNK_CHARS",
    "FOLD_WINDOW",
    "MAX_LINE_CHARS",
    "SLICE_LINES",
    "TEXT_CAP",
    "CodeEditor",
    "FoldModel",
    "LineRanges",
    "PrettyText",
    "TextView",
    "build_pretty",
    "find_matches",
    "line_starts",
    "node_path_at",
    "qt_mime_data",
    "scan_folds",
]

#: Pretty text shown at most (bytes of UTF-8 output). A QTextDocument costs ~20x its text
#: (one block per line), so 4 MiB (~80 MB of document, ~1 s to load) keeps the view within the
#: GUI memory budget (ARCHITECTURE §11.0); bigger nodes show the beginning + "open in raw view".
TEXT_CAP = 4 * 1024 * 1024
#: Display lines are shortened to this many characters.
MAX_LINE_CHARS = 10_000
#: Characters inserted into the editor per step (loading, unfolding).
CHUNK_CHARS = 32 * 1024
#: Document lines removed per folding step.
SLICE_LINES = 20_000
#: Documents with more lines are emptied in slices of this many lines when replaced.
RETIRE_BLOCKS = 8_000
#: Range boundaries / fold ranges examined per step at most.
MAX_SCAN = 4096
#: Bulk fold changes with more ranges than this reload the shown lines instead of
#: inserting / removing range by range.
REBUILD_RANGES = 64
#: Seconds of insertion / folding steps per event-loop turn (timer) and right after a
#: user action (small texts finish at once).
TICK_BUDGET_S = 0.012
SYNC_BUDGET_S = 0.025
#: A bulk fold change whose computation took longer changes the document on the next turn.
DEFER_AFTER_S = 0.03
#: The node at the caret is selected this long after the last caret move.
CARET_DEBOUNCE_MS = 250
#: Pretty texts kept for switching back to a node.
PRETTY_CACHE = 2
#: Texts up to this many characters are searched in the GUI thread.
SYNC_FIND_CHARS = 1_000_000
FIND_MAX_MATCHES = 100_000
INDENTS: tuple[tuple[str, int | str], ...] = (("2", 2), ("4", 4), ("tab", "\t"))
STATE_INDENT = "text_indent"
STATE_WRAP = "text_wrap"
FOLD_LEVELS = 5
_READ_CHUNK = 1 << 20
_STARTS_STEP = 1 << 20
_MIN_POINT = 6.0
_MAX_POINT = 40.0
_FAR = 1 << 62
ELLIPSIS = "…"


# ==================================================================================
# folds (pure Python, worker thread)
# ==================================================================================
class FoldModel:
    """Foldable regions of a pretty-printed text: for each line that opens a non-empty
    object / array (it ends with ``{`` / ``[``) the line that closes it (it starts with
    ``}`` / ``]``; ``-1`` when the text ends first), its nesting depth (0 = the outermost
    container), its parent region (``-1``: none), its kind (``kinds[i]`` 1 = array, 0 =
    object) and ``sib_before[i]``: the lines the preceding sibling regions take beyond
    their first line (to count the members / elements before a line)."""

    __slots__ = ("opens", "closes", "depths", "close_lines", "close_opens", "lines", "parents", "sib_before",
                 "kinds")

    def __init__(self, opens: array | None = None, closes: array | None = None, depths: array | None = None,
                 close_lines: array | None = None, close_opens: array | None = None, lines: int = 0,
                 parents: array | None = None, sib_before: array | None = None,
                 kinds: bytearray | None = None) -> None:
        self.opens = opens if opens is not None else array("i")
        self.closes = closes if closes is not None else array("i")
        self.depths = depths if depths is not None else array("i")
        self.close_lines = close_lines if close_lines is not None else array("i")
        self.close_opens = close_opens if close_opens is not None else array("i")
        self.lines = lines
        self.parents = parents if parents is not None else array("i", [-1] * len(self.opens))
        self.sib_before = sib_before if sib_before is not None else array("i", [0] * len(self.opens))
        self.kinds = kinds if kinds is not None else bytearray(len(self.opens))

    def __len__(self) -> int:
        return len(self.opens)

    def index(self, line: int) -> int | None:
        """The region opened at ``line`` (``None``: no region opens there)."""
        i = bisect.bisect_left(self.opens, line)
        if i < len(self.opens) and self.opens[i] == line:
            return i
        return None

    _index = index

    def close_of(self, line: int) -> int | None:
        """The closing line of the region opened at ``line`` (``None``: no region / never closed)."""
        i = self.index(line)
        if i is None:
            return None
        c = self.closes[i]
        return c if c > line else None

    def depth_of(self, line: int) -> int | None:
        i = self.index(line)
        return None if i is None else int(self.depths[i])

    def open_of(self, close_line: int) -> int | None:
        """The opening line of the region closed at ``close_line``."""
        i = bisect.bisect_left(self.close_lines, close_line)
        if i < len(self.close_lines) and self.close_lines[i] == close_line:
            return int(self.close_opens[i])
        return None

    def regions(self) -> Iterator[tuple[int, int, int]]:
        """``(open, close, depth)`` of every closed region, in line order."""
        for o, c, d in zip(self.opens, self.closes, self.depths):
            if c > o:
                yield o, c, d


_FOLD_RE = re.compile(r"(?P<o>[\[{])$|^[ \t]*(?P<c>[\]}])", re.M)
_CLOSE_RE = re.compile(r"[ \t]*[\]}]")
#: Characters :func:`scan_folds` examines per regular-expression call at most (``re`` holds
#: the GIL for a whole search: one search over a flat 4 MiB array took ~55 ms, during which
#: the GUI thread could not run).
FOLD_WINDOW = 1 << 16


def scan_folds(text: str) -> tuple[FoldModel, bool]:
    """Fold regions of pretty JSON text (``core.Reformatter`` layout: a non-empty
    container's opening bracket ends its line, its closing bracket starts a line).
    Returns ``(model, balanced)``; ``balanced`` is False when a container is not closed
    (the text was cut, or the JSON is truncated).

    The text is searched in windows of whole lines of at most :data:`FOLD_WINDOW`
    characters (the GIL is free between them); of a line longer than that only its start
    and its last character are looked at -- the only places a bracket can match."""
    opens, closes, depths = array("i"), array("i"), array("i")
    close_lines, close_opens = array("i"), array("i")
    parents, sib_before, kinds = array("i"), array("i"), bytearray()
    stack: list[int] = []
    spans = [0]                      # per open container (plus the top level): spans of its closed child regions

    def opened(line: int, kind: str) -> None:
        parents.append(stack[-1] if stack else -1)
        sib_before.append(spans[-1])
        kinds.append(1 if kind == "[" else 0)
        stack.append(len(opens))
        spans.append(0)
        opens.append(line)
        closes.append(-1)
        depths.append(len(stack) - 1)

    def closed(line: int) -> None:
        if stack:
            i = stack.pop()
            spans.pop()
            closes[i] = line
            spans[-1] += line - opens[i]
            close_lines.append(line)
            close_opens.append(opens[i])

    n = len(text)
    count, find, rfind = text.count, text.find, text.rfind
    window = max(1, int(FOLD_WINDOW))
    finditer = _FOLD_RE.finditer
    pos = 0                          # a line start
    line = 0                         # the line at ``pos``
    while pos < n:
        if pos + window >= n:
            end = n
        else:
            cut = rfind("\n", pos, pos + window)
            if cut < 0:              # the line at ``pos`` is longer than a window
                e = find("\n", pos)
                e = n if e < 0 else e
                if _CLOSE_RE.match(text, pos, e) is not None:
                    closed(line)
                if text[e - 1] in "[{":
                    opened(line, text[e - 1])
                pos = e + 1
                line += 1
                continue
            end = cut + 1
        last = pos
        for m in finditer(text, pos, end):
            s = m.start()
            line += count("\n", last, s)
            last = s
            if m.lastgroup == "o":
                opened(line, m.group("o"))
            else:
                closed(line)
        line += count("\n", last, end)
        pos = end
    lines = count("\n") + 1
    return FoldModel(opens, closes, depths, close_lines, close_opens, lines, parents, sib_before, kinds), not stack


def line_starts(text: str) -> array:
    """Start offsets of the lines of ``text`` plus the sentinel ``len(text) + 1``: line ``i``
    is ``text[starts[i]:starts[i + 1] - 1]`` (``"\\n"``-separated; worker threads, about
    50 ms for 16 M characters, little temporary memory)."""
    out = array("i", [0])
    n = len(text)
    pos = 0
    while pos < n:
        cut = text.find("\n", min(n, pos + _STARTS_STEP))
        end = n if cut < 0 else cut + 1
        parts = text[pos:end].split("\n")
        it = accumulate(map(add, map(len, parts[:-1]), repeat(1)), initial=pos)
        next(it)
        out.extend(it)
        pos = end
    out.append(n + 1)
    return out


# ==================================================================================
# line ranges (pure Python): folded lines <-> document blocks
# ==================================================================================
class LineRanges:
    """Sorted, disjoint, inclusive ranges of source lines (``a[k]..b[k]``) with prefix
    sums: which lines are left out of the document and where the remaining lines sit.

    ``before(x)``: ranged lines before ``x``; ``to_source(d)``: the source line of
    document line ``d`` (``d``-th line not in a range). Adjacent ranges merge."""

    __slots__ = ("a", "b", "cum", "ds")

    def __init__(self) -> None:
        self.a: list[int] = []
        self.b: list[int] = []
        self.cum: list[int] = [0]      # lines in the ranges before range k
        self.ds: list[int] = []        # document line at which range k sits (a[k] - cum[k])

    @classmethod
    def from_ranges(cls, ranges: list[tuple[int, int]]) -> LineRanges:
        """From sorted ranges (C-level loops when they are disjoint and not adjacent)."""
        out = cls()
        if not ranges:
            return out
        A = [r[0] for r in ranges]
        B = [r[1] for r in ranges]
        if all(map(le, A, B)) and all(map(lt, map(add, B[:-1], repeat(1)), A[1:])):
            return cls.from_lists(A, B)
        for a, b in ranges:
            out.append(a, b)
        return out

    @classmethod
    def from_lists(cls, a: list[int], b: list[int]) -> LineRanges:
        """From starts and ends of sorted, disjoint, non-adjacent ranges (taken over)."""
        out = cls()
        out.a, out.b = a, b
        out.cum = list(accumulate(map(add, map(sub, b, a), repeat(1)), initial=0))
        out.ds = list(map(sub, a, out.cum))
        return out

    def __len__(self) -> int:
        return len(self.a)

    def ranges(self) -> list[tuple[int, int]]:
        return list(zip(self.a, self.b))

    def total(self) -> int:
        return self.cum[-1]

    def find(self, x: int) -> int:
        """Index of the range containing ``x`` (``-1``: none)."""
        k = bisect.bisect_right(self.a, x) - 1
        return k if k >= 0 and x <= self.b[k] else -1

    def run_at(self, x: int) -> tuple[bool, int]:
        """``(in a range, last line of the run of lines in the same state)``."""
        k = bisect.bisect_right(self.a, x) - 1
        if k >= 0 and x <= self.b[k]:
            return True, self.b[k]
        return False, (self.a[k + 1] - 1 if k + 1 < len(self.a) else _FAR)

    def next_start(self, x: int) -> int:
        """The first range start after ``x`` (a huge number: none)."""
        k = bisect.bisect_right(self.a, x)
        return self.a[k] if k < len(self.a) else _FAR

    def before(self, x: int) -> int:
        """Number of ranged lines below ``x``."""
        k = bisect.bisect_right(self.a, x) - 1
        if k < 0:
            return 0
        if x <= self.b[k]:
            return self.cum[k] + x - self.a[k]
        return self.cum[k + 1]

    def to_source(self, d: int) -> int:
        """The ``d``-th (0-based) line that is in no range."""
        return d + self.cum[bisect.bisect_right(self.ds, d)]

    def next_free(self, x: int) -> int:
        """The first line after ``x`` in no range (``x`` itself in no range)."""
        y = x + 1
        k = self.find(y)
        return self.b[k] + 1 if k >= 0 else y

    def clip(self, lo: int, hi: int) -> list[tuple[int, int]]:
        """``(starts, ends)`` of the ranges intersecting ``lo..hi``, cut to it."""
        k0 = bisect.bisect_left(self.b, lo)
        k1 = bisect.bisect_right(self.a, hi)
        if k0 >= k1:
            return [], []
        a = self.a[k0:k1]
        b = self.b[k0:k1]
        a[0] = max(a[0], lo)
        b[-1] = min(b[-1], hi)
        return a, b

    def append(self, a: int, b: int) -> None:
        """Add ``a..b`` after every range (merged with the last one when adjacent)."""
        if b < a:
            return
        if self.a and a <= self.b[-1] + 1:
            if b > self.b[-1]:
                self.cum[-1] += b - self.b[-1]
                self.b[-1] = b
            return
        self.ds.append(a - self.cum[-1])
        self.a.append(a)
        self.b.append(b)
        self.cum.append(self.cum[-1] + b - a + 1)

    def replace(self, lo: int, hi: int, starts: list[int], ends: list[int]) -> None:
        """Make the coverage of ``lo..hi`` exactly the ranges ``starts[i]..ends[i]``
        (sorted, disjoint, inside ``lo..hi``); everything outside stays."""
        A, B = self.a, self.b
        k0 = bisect.bisect_left(B, lo)
        k1 = bisect.bisect_right(A, hi)
        ma: list[int] = []
        mb: list[int] = []
        if k0 < k1 and A[k0] < lo:
            ma.append(A[k0])
            mb.append(lo - 1)
        ma.extend(starts)
        mb.extend(ends)
        if k0 < k1 and B[k1 - 1] > hi:
            ma.append(hi + 1)
            mb.append(B[k1 - 1])
        if k0 > 0 and ma and B[k0 - 1] + 1 >= ma[0]:
            k0 -= 1
            ma.insert(0, A[k0])
            mb.insert(0, B[k0])
        if k1 < len(A) and ma and mb[-1] + 1 >= A[k1]:
            ma.append(A[k1])
            mb.append(B[k1])
            k1 += 1
        if not (all(map(le, ma, mb)) and all(map(lt, map(add, mb[:-1], repeat(1)), ma[1:]))):
            ra: list[int] = []
            rb: list[int] = []
            for a, b in zip(ma, mb):
                if b < a:
                    continue
                if ra and a <= rb[-1] + 1:
                    if b > rb[-1]:
                        rb[-1] = b
                else:
                    ra.append(a)
                    rb.append(b)
            ma, mb = ra, rb
        A[k0:k1] = ma
        B[k0:k1] = mb
        self.cum = list(accumulate(map(add, map(sub, B, A), repeat(1)), initial=0))
        self.ds = list(map(sub, A, self.cum))


# ==================================================================================
# building the text (worker thread)
# ==================================================================================
#: Characters shown as a visible symbol, one for one (positions in code points do not move).
#: Besides controls this covers every character a ``QTextDocument`` turns into a block
#: (paragraph / frame) boundary when inserted -- CR, U+2029 and its frame markers U+FDD0 /
#: U+FDD1 (an exhaustive test checks that nothing else splits a block) -- the line
#: separator U+2028 (a line break inside a block) and the other Unicode noncharacters.
_DISPLAY_MAP = {c: 0x2400 + c for c in range(0x20) if c not in (0x09, 0x0A)}
_DISPLAY_MAP[0x7F] = 0x2421
_DISPLAY_MAP.update({c: 0x00B7 for c in range(0x80, 0xA0)})
_DISPLAY_MAP.update({0x2028: 0x21B5, 0x2029: 0x00B6})
_DISPLAY_MAP.update({c: 0xFFFD for c in range(0xFDD0, 0xFDF0)})
_DISPLAY_MAP.update({(plane << 16) | low: 0xFFFD for plane in range(17) for low in (0xFFFE, 0xFFFF)})
_DISPLAY_TABLE = {k: chr(v) for k, v in _DISPLAY_MAP.items()}
_DISPLAY_TRANS = str.maketrans(_DISPLAY_TABLE)
_LONG_RES: dict[int, re.Pattern[str]] = {}
_KEY_RE = re.compile(r'[ \t]*("(?:[^"\\\n]|\\.)*")[ \t]*:')


def _display_piece(text: str, limit: int) -> tuple[str, int]:
    """``text`` with its characters made visible (:data:`_DISPLAY_MAP`) and the lines
    longer than ``limit`` characters shortened to ``limit`` + ``…``; ``(text, lines
    shortened)``. The kept slices are mapped one by one: after the cut (a huge line costs
    only its shown part) and before the ``…`` joins them (``str.translate`` is much faster
    on ASCII-only text). The mapping is one for one: positions do not move."""
    pat = _LONG_RES.get(limit)
    if pat is None:
        pat = _LONG_RES[limit] = re.compile(r"(?m)^[^\n]{%d}" % (limit + 1))
    parts: list[str] = []
    pos = 0
    n = 0
    search = pat.search
    while True:
        m = search(text, pos)                # the next search starts after the long line
        if m is None:
            break
        s = m.start()
        e = text.find("\n", s)
        if e < 0:
            e = len(text)
        parts.append(text[pos:s + limit].translate(_DISPLAY_TRANS))
        parts.append(ELLIPSIS)
        pos = e
        n += 1
    if not n:
        return text.translate(_DISPLAY_TRANS), 0
    parts.append(text[pos:].translate(_DISPLAY_TRANS))
    return "".join(parts), n


def _display_text(text: str, max_line: int, cancel: CancelToken | None = None) -> tuple[str, int]:
    """Control characters made visible and long lines shortened (:func:`_display_piece`),
    piece by piece (about 1 MiB each: no single long C call holds the GIL while the GUI
    thread waits)."""
    n = len(text)
    if n <= _STARTS_STEP:
        return _display_piece(text, max_line)
    parts: list[str] = []
    cut = 0
    pos = 0
    while pos < n:
        check(cancel)
        nl = text.find("\n", min(n, pos + _STARTS_STEP))
        end = n if nl < 0 else nl + 1
        piece, k = _display_piece(text[pos:end], max_line)
        parts.append(piece)
        cut += k
        pos = end
    return "".join(parts), cut


@dataclass
class PrettyText:
    """What :func:`build_pretty` produced."""

    text: str = ""                 # display text (control characters visible, long lines shortened)
    full: str | None = None        # the complete pretty text when it differs from ``text``
    folds: FoldModel = field(default_factory=FoldModel)
    kind: str = ""                 # node kind
    start: int = 0                 # node span in the data file
    size: int = 0
    truncated: bool = False        # only the first TEXT_CAP bytes of output
    shown_bytes: int = 0           # UTF-8 bytes of the pretty text kept
    error: Any = None              # the document's JsonSyntaxError when it lies inside the node
    error_offset: int | None = None
    cut_lines: int = 0             # display lines shortened
    virtual: bool = False          # the synthetic root of a JSON Lines / stream document
    records: int = 0               # virtual root: records indexed
    starts: array | None = None    # line starts of ``text`` (line_starts)
    full_starts: array | None = None   # line starts of ``full`` when lines were shortened
    ipath: tuple | None = None     # child indices along the node's path (None: unknown)

    @property
    def copy_text(self) -> str:
        return self.full if self.full is not None else self.text

    def line_starts(self) -> array:
        if self.starts is None:
            self.starts = line_starts(self.text)
        return self.starts

    def _full_starts(self) -> array:
        if self.full_starts is not None:
            return self.full_starts
        return self.line_starts()      # translated characters keep their positions

    def line_count(self) -> int:
        return len(self.line_starts()) - 1

    def line_text(self, line: int) -> str:
        """Display text of ``line``."""
        s = self.line_starts()
        return self.text[s[line]:s[line + 1] - 1]

    def full_line(self, line: int) -> str:
        """Complete text of ``line``."""
        s = self._full_starts()
        return self.copy_text[s[line]:s[line + 1] - 1]

    def full_offset(self, line: int, col: int) -> int:
        """Offset in :attr:`copy_text` of the display position ``(line, col)`` (code
        points); a position in or after the ``…`` of a shortened line is the line end."""
        line = max(0, min(int(line), self.line_count() - 1))
        ds, fs = self.line_starts(), self._full_starts()
        dlen = ds[line + 1] - 1 - ds[line]
        flen = fs[line + 1] - 1 - fs[line]
        col = max(0, int(col))
        if flen != dlen and col >= dlen:
            col = flen
        return fs[line] + min(col, flen)

    def copy_range(self, l0: int, c0: int, l1: int, c1: int) -> str:
        """The complete text between two display positions."""
        a, b = self.full_offset(l0, c0), self.full_offset(l1, c1)
        return self.copy_text[min(a, b):max(a, b)]

    def member_key(self, line: int) -> str | None:
        """The object key written at the start of ``line`` (``None``: no key)."""
        m = _KEY_RE.match(self.full_line(line))
        if m is None:
            return None
        try:
            key = json.loads(m.group(1), strict=False)
        except ValueError:
            return None
        return key if isinstance(key, str) else None


def node_path_at(pretty: PrettyText, line: int) -> tuple[tuple, tuple] | None:
    """``(path, child indices)`` of the value written on ``line``, relative to the node of
    ``pretty`` (a closing line belongs to its container); ``None`` when unknown. Runs in
    O(depth + log n) from the fold regions -- no per-line table."""
    fm = pretty.folds
    if pretty.virtual or not 0 <= line < max(1, fm.lines):
        return None
    o = fm.open_of(line)
    if o is not None:
        line = o
    opens, closes, parents, sib, kinds = fm.opens, fm.closes, fm.parents, fm.sib_before, fm.kinds
    child_region = fm.index(line)
    r = bisect.bisect_left(opens, line) - 1          # the last region opening above the line
    while r >= 0 and 0 <= closes[r] < line:
        r = parents[r]
    segs: list[Any] = []
    idxs: list[int] = []
    child = line
    while r >= 0:
        top = opens[r]
        if child_region is not None:
            hidden = sib[child_region]
        else:
            a = bisect.bisect_left(opens, child) - 1
            while a >= 0 and a != r and parents[a] != r:
                a = parents[a]
            hidden = 0 if a < 0 or a == r else sib[a] + closes[a] - opens[a]
        idx = child - top - 1 - hidden
        if idx < 0:
            return None
        if kinds[r]:
            segs.append(idx)
        else:
            key = pretty.member_key(child)
            if key is None:
                return None
            segs.append(key)
        idxs.append(idx)
        child, child_region = top, r
        r = parents[r]
    segs.reverse()
    idxs.reverse()
    return tuple(segs), tuple(idxs)


def build_pretty(session: Any, ref: Any, indent: int | str = 2, cancel: CancelToken | None = None, *,
                 cap: int = TEXT_CAP, max_line: int = MAX_LINE_CHARS) -> PrettyText:
    """Pretty text of ``ref`` (worker threads only): the node's bytes through one
    ``core.Reformatter(indent)`` (read in 1 MiB pieces, stopped once ``cap`` bytes of
    output exist; a JSON error inside the node stops at the error), fold ranges, display
    clean-up, line starts. ``UserInputError`` when the node does not exist."""
    from jsonreshape.core import Reformatter

    node = session.node(ref, cancel)
    doc = node.doc
    if getattr(node, "is_virtual", False):
        return PrettyText(kind=node.kind, start=node.start, size=node.size, virtual=True,
                          records=int(doc.record_count))
    ipath = _node_ipath(session, ref, cancel)
    start, end = int(node.start), int(node.end)
    err = doc.error
    eo = getattr(doc, "error_data_offset", None) if err is not None else None
    stop = eo if eo is not None and start <= eo < end else end
    fmt = Reformatter(indent=indent)
    out = bytearray()
    pos = start
    with doc.open_data() as f:
        f.seek(pos)
        while pos < stop and len(out) <= cap:
            check(cancel)
            chunk = f.read(min(_READ_CHUNK, stop - pos))
            if not chunk:
                raise FileChangedError(path=str(getattr(doc, "source_path", "")))
            pos += len(chunk)
            out += fmt.feed(chunk)
    consumed = pos >= stop
    if consumed:
        out += fmt.finish()
    truncated = len(out) > cap or not consumed
    if len(out) > cap:
        cut = out.rfind(b"\n", 0, cap + 1)
        del out[cut if cut >= cap // 2 else cap:]
    check(cancel)
    text = codecs.utf_8_decode(bytes(out), "replace", False)[0]   # a character cut at the end is left out
    shown = len(out)
    del out
    folds, balanced = scan_folds(text)
    check(cancel)
    error = None
    if eo is not None and start <= eo and (eo < end or (not truncated and not balanced)):
        error = err
    display, cut_lines = _display_text(text, max_line, cancel)
    full = None if display == text else text
    check(cancel)
    starts = line_starts(display)
    full_starts = line_starts(text) if cut_lines else None
    return PrettyText(text=display, full=full, folds=folds, kind=node.kind, start=start, size=end - start,
                      truncated=truncated, shown_bytes=shown, error=error,
                      error_offset=eo if error is not None else None, cut_lines=cut_lines, starts=starts,
                      full_starts=full_starts, ipath=ipath)


def _node_ipath(session: Any, ref: Any, cancel: CancelToken | None) -> tuple | None:
    """The child indices along ``ref``'s path (``ref.ipath`` when complete, else resolved
    like :meth:`DocSession.node` resolves the path; ``None`` when unknown). With them the
    node at the caret is addressed exactly, also among duplicate keys."""
    path = tuple(getattr(ref, "path", ()) or ())
    ipath = getattr(ref, "ipath", None)
    if ipath is not None and len(ipath) == len(path):
        return tuple(ipath)
    if not path:
        return ()
    resolve = getattr(session, "resolve", None)
    if resolve is None:
        return None
    try:
        found = resolve(ref, cancel)
    except (Cancelled, FileChangedError):
        raise
    except JsonReshapeError as exc:            # the text is shown without exact node identities
        log.debug("text view: resolving %r failed: %r", ref, exc)
        return None
    got = getattr(found, "ipath", None)
    return tuple(got) if got is not None and len(got) == len(path) else None


@dataclass
class Matches:
    """Quick-find matches: line, column and length in characters (code points)."""

    lines: array = field(default_factory=lambda: array("i"))
    cols: array = field(default_factory=lambda: array("i"))
    lens: array = field(default_factory=lambda: array("i"))
    capped: bool = False

    def __len__(self) -> int:
        return len(self.lines)


def find_matches(text: str, matcher: Any, cap: int = FIND_MAX_MATCHES,
                 cancel: CancelToken | None = None) -> Matches:
    """Match spans of ``matcher`` in ``text`` as (line, column, length) per line."""
    out = Matches()
    spans = matcher(text)
    line = 0
    last = 0
    line_start = 0
    for k, (s, e) in enumerate(spans):
        if k >= cap:
            out.capped = True
            break
        if k % 4096 == 0:
            check(cancel)
        nl = text.count("\n", last, s)
        if nl:
            line += nl
            line_start = text.rfind("\n", 0, s) + 1
        last = s
        end_line = text.find("\n", s, e)
        if end_line >= 0:
            e = end_line
        out.lines.append(line)
        out.cols.append(s - line_start)
        out.lens.append(max(0, e - s))
    return out


# ==================================================================================
# syntax colors (per block)
# ==================================================================================
_TOKEN_RE = re.compile(
    r'"[^"\\]*(?:\\.[^"\\]*)*"(?P<colon>[ \t]*:)?'
    r'|(?P<open>"[^"\\]*(?:\\.[^"\\]*)*\\?$)'
    r'|(?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'
    r'|(?P<lit>true|false|null)'
    r'|(?P<punct>[{}\[\],:])')
_ASTRAL_RE = re.compile("[\U00010000-\U0010ffff]")


def _u16(text: str, i: int) -> int:
    """UTF-16 position of code point ``i`` of ``text`` (Qt positions)."""
    if i <= 0:
        return 0
    head = text[:i]
    if _ASTRAL_RE.search(head) is None:
        return len(head)
    return len(head) + len(_ASTRAL_RE.findall(head))


def _cp(text: str, u: int) -> int:
    """Code point index of the UTF-16 position ``u`` in ``text``."""
    if _ASTRAL_RE.search(text) is None:
        return min(u, len(text))
    pos = 0
    for i, ch in enumerate(text):
        if pos >= u:
            return i
        pos += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def line_tokens(line: str) -> list[tuple[int, int, str]]:
    """``(start, end, role)`` color runs of one line of pretty JSON (code points)."""
    out: list[tuple[int, int, str]] = []
    for m in _TOKEN_RE.finditer(line):
        s, e = m.span()
        if m.group("punct") is not None:
            out.append((s, e, "punct"))
        elif m.group("num") is not None:
            out.append((s, e, "number"))
        elif m.group("lit") is not None:
            lit = m.group("lit")
            out.append((s, e, "null" if lit == "null" else "bool"))
        elif m.group("open") is not None:
            out.append((s, e, "string"))
        else:
            colon = m.start("colon") if m.group("colon") is not None else -1
            if colon >= 0:
                out.append((s, colon, "key"))
                out.append((e - 1, e, "punct"))
            else:
                out.append((s, e, "string"))
    return out


def qt_mime_data(text: str) -> QMimeData:
    """A ``QMimeData`` holding ``text`` (``text/plain`` only) that Qt itself created.

    Qt owns the mime data of a copy or a drag and deletes it when it likes: the clipboard
    keeps it until the next copy or until the process ends, after the interpreter has
    finished. A ``QMimeData()`` made from Python is a binding subclass whose destructor
    calls into the interpreter, so deleting it then crashed the process at exit. A plain
    one comes from ``QAbstractItemModel.mimeData`` (its item data is dropped); no Python
    reference to it is kept."""
    model = QStringListModel([""])
    data = model.mimeData([model.index(0, 0)])
    data.clear()
    data.setText(text)
    return data


# ==================================================================================
# the editor
# ==================================================================================
class _Gutter(QWidget):
    """Line numbers + fold markers left of the editor."""

    def __init__(self, editor: CodeEditor) -> None:
        super().__init__(editor)
        self._editor = editor
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return QSize(self._editor.gutter_width(), 0)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt name)
        self._editor.paint_gutter(self, event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        if event.button() == Qt.MouseButton.LeftButton:
            self._editor.gutter_clicked(event.position().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)


class _Goal(NamedTuple):
    """A position to show once its lines are in the document: ``top`` (first shown line),
    ``caret`` (caret line), ``select`` (``(line, column, length)`` to select)."""

    top: int | None = None
    caret: int | None = None
    select: tuple[int, int, int] | None = None


class CodeEditor(QPlainTextEdit):
    """Read-only JSON editor: gutter, lazy colors, folding, extra highlights. Line
    numbers in its API are *source lines* (lines of the text); the document holds the
    lines that are not folded away."""

    content_loaded = Signal()          # every line of the text is in the document (or folded away)
    folds_changed = Signal()
    caret_moved = Signal()             # the user moved the caret (keys / mouse)

    def __init__(self, view: TextView) -> None:
        super().__init__(view)
        self._view = view
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                     | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.setCenterOnScroll(False)
        self.folds = FoldModel()
        self.folded: set[int] = set()
        self._text = ""
        self._starts = array("i", [0, 1])
        self._total = 1
        self._loaded = 1
        self._hidden = LineRanges()        # source lines not in the document (loaded part)
        self._target = LineRanges()        # source lines that should not be (outer folded regions)
        self._jobs: list[list[int]] = []   # [cursor, last line]: parts converging to the target
        self._rebuild_due = False          # the shown lines are inserted again from the start (bulk folds)
        self._goal: _Goal | None = None
        self._just_loaded = False
        self._progress = -1
        self._version = 1
        self._formats: dict[str, QTextCharFormat] = {}
        self._work_timer = QTimer(self)
        self._work_timer.setInterval(0)
        self._work_timer.timeout.connect(self._work_tick)
        self._retired: list[QTextDocument] = []
        self._retire_timer = QTimer(self)
        self._retire_timer.setInterval(0)
        self._retire_timer.timeout.connect(self._retire_tick)
        self._extras_due = False
        self._placeholders: list[tuple[QRectF, int]] = []
        self._colors_cache: dict[str, QColor] = {}
        self._gutter_w = -1
        self._empty_text = ""
        self.gutter = _Gutter(self)
        self.updateRequest.connect(self._on_update_request)
        self.cursorPositionChanged.connect(self.schedule_extras)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self.schedule_extras())
        theme.notifier().theme_changed.connect(self.refresh_colors)
        self._update_gutter_width()

    # ------------------------------------------------------------------ content / lines
    @property
    def loading(self) -> bool:
        """Lines of the text are still to be inserted."""
        return self._loaded < self._total

    def busy(self) -> bool:
        """Insertion or folding steps are pending."""
        return self._loaded < self._total or bool(self._jobs) or self._rebuild_due

    def loaded_lines(self) -> int:
        """Source lines handled so far (in the document or folded away)."""
        return self._loaded

    def line_count(self) -> int:
        """Source lines of the text."""
        return self._total

    def block_of_line(self, line: int) -> int | None:
        """The document block of source ``line`` (``None``: folded away or not loaded yet)."""
        if not 0 <= line < self._loaded or self._hidden.find(line) >= 0:
            return None
        return line - self._hidden.before(line)

    def line_of_block(self, n: int) -> int:
        """The source line of document block ``n``."""
        return self._hidden.to_source(max(0, n))

    def block_for_line(self, line: int) -> QTextBlock:
        n = self.block_of_line(line)
        return self.document().findBlockByNumber(n) if n is not None else QTextBlock()

    def current_line(self) -> int:
        """Source line of the caret."""
        return self.line_of_block(self.textCursor().blockNumber())

    def shown_lines(self) -> list[int]:
        """Source lines in the document, in order (tests)."""
        out = []
        block = self.document().begin()
        line = self.line_of_block(0)
        while block.isValid():
            out.append(line)
            line = self._hidden.next_free(line)
            block = block.next()
        return out

    def line_text(self, line: int) -> str:
        """Display text of source ``line``."""
        s = self._starts
        if not 0 <= line < self._total:
            return ""
        return self._text[s[line]:s[line + 1] - 1]

    def set_content(self, text: str, folds: FoldModel, *, starts: array | None = None,
                    folded: set[int] | None = None, goal: _Goal | None = None) -> None:
        """Show ``text`` (``folded``: regions folded from the start; ``goal``: a position
        shown once its lines are inserted). Big texts are inserted over several
        event-loop turns."""
        self._work_timer.stop()
        self._jobs = []
        self._rebuild_due = False
        self._text = text
        self._starts = starts if starts is not None else line_starts(text)
        self.folds = folds
        self._total = max(1, len(self._starts) - 1)
        self.folded = self._closed_folds(folded) if folded else set()
        self._target = LineRanges.from_lists(*self._outer_regions())
        self._hidden = LineRanges()
        self._loaded = 0
        self._goal = goal
        self._placeholders = []
        self._progress = -1
        self._fresh_document()    # first: a new gutter width must not make Qt lay out the old text again
        self._update_gutter_width()
        self._run(0.0)            # the first chunk now (a small text completely), the rest on the timer
        self.schedule_extras()

    def clear_content(self) -> None:
        self.set_content("", FoldModel(), starts=array("i", [0, 1]))

    def _fit(self, y: int, last: int, room: int, first: bool) -> int:
        """The last line ``e`` in ``y..last`` with lines ``y..e`` within ``room`` characters
        (``y`` when ``first`` and not even one fits; ``y - 1``: none)."""
        s = self._starts
        m = bisect.bisect_right(s, s[y] + room, y + 1, last + 2) - 1
        e = m - 1
        if e < y:
            return y if first else y - 1
        return e

    def _join(self, segs: list[tuple[int, int]]) -> str:
        s, text = self._starts, self._text
        return "\n".join(text[s[a]:s[b + 1] - 1] for a, b in segs)

    def _load_chunk(self) -> None:
        """Append the next lines of the text (at most CHUNK_CHARS characters; folded
        regions are skipped without inserting anything)."""
        start, total = self._loaded, self._total
        if start >= total:
            return
        target = self._target
        segs: list[tuple[int, int]] = []
        skipped: list[tuple[int, int]] = []
        y = start
        room = CHUNK_CHARS
        scans = 0
        while y < total and scans < MAX_SCAN:
            scans += 1
            k = target.find(y)
            if k >= 0:
                e = min(target.b[k], total - 1)
                skipped.append((y, e))
                y = e + 1
                continue
            last = min(target.next_start(y), total) - 1
            e = self._fit(y, last, room, not segs)
            if e < y:
                break
            segs.append((y, e))
            room -= self._starts[e + 1] - self._starts[y]
            y = e + 1
            if e < last:
                break
        if segs:
            chunk = self._join(segs)
            if start == 0:
                self.setPlainText(chunk)
            else:
                doc = self.document()
                doc.lastBlock().setUserState(-1)      # its layout changes: color it again
                cursor = QTextCursor(doc)
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText("\n" + chunk)
        for a, b in skipped:
            self._hidden.append(a, b)
        self._loaded = y
        if y >= total:
            self._just_loaded = True

    def _remove_blocks(self, d0: int, d1: int) -> None:
        """Remove document lines ``d0..d1`` (``d0 >= 1``)."""
        doc = self.document()
        prev = doc.findBlockByNumber(d0 - 1)
        last = doc.findBlockByNumber(d1)
        cursor = QTextCursor(doc)
        cursor.setPosition(prev.position() + prev.length() - 1)
        cursor.setPosition(last.position() + last.length() - 1, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        prev.setUserState(-1)

    def _insert_after(self, n: int, text: str) -> None:
        """Insert lines after document line ``n``."""
        doc = self.document()
        block = doc.findBlockByNumber(n)
        cursor = QTextCursor(doc)
        cursor.setPosition(block.position() + block.length() - 1)
        cursor.insertText("\n" + text)
        block.setUserState(-1)

    def _hide_step(self, x: int, e: int) -> int:
        """Remove the document lines of ``x..e`` (``x`` in the document), at most
        SLICE_LINES; returns the last source line handled."""
        hidden = self._hidden
        hb = hidden.before(x)
        d0 = x - hb
        shown = (e - x + 1) - (hidden.before(e + 1) - hb)
        if shown > SLICE_LINES:
            shown = SLICE_LINES
            e = hidden.to_source(d0 + shown - 1)
        if shown > 0:
            self._remove_blocks(d0, d0 + shown - 1)
        hidden.replace(x, e, [x], [e])
        return e

    def _show_step(self, x: int, end: int) -> int:
        """Insert the lines of ``x..end`` (out of the document) that the target shows,
        at most CHUNK_CHARS characters; returns the last source line handled."""
        target = self._target
        segs: list[tuple[int, int]] = []
        y = x
        room = CHUNK_CHARS
        scans = 0
        while y <= end and scans < MAX_SCAN:
            scans += 1
            k = target.find(y)
            if k >= 0:
                y = min(target.b[k], end) + 1
                continue
            last = min(target.next_start(y) - 1, end)
            e = self._fit(y, last, room, not segs)
            if e < y:
                break
            segs.append((y, e))
            room -= self._starts[e + 1] - self._starts[y]
            y = e + 1
            if e < last:
                break
        z = y - 1
        hidden = self._hidden
        if segs:
            self._insert_after(x - hidden.before(x) - 1, self._join(segs))
        hidden.replace(x, z, *target.clip(x, z))
        return z

    def _converge_step(self, job: list[int]) -> bool:
        """One bounded step making the document match the target in ``job``'s lines;
        True when they match."""
        hidden, target = self._hidden, self._target
        x, hi = job
        hi = min(hi, self._loaded - 1)
        scans = 0
        while x <= hi:
            h_in, h_end = hidden.run_at(x)
            t_in, t_end = target.run_at(x)
            if h_in == t_in:
                x = min(h_end, t_end) + 1
                scans += 1
                if scans >= MAX_SCAN:
                    job[0] = x
                    return False
                continue
            if t_in:
                job[0] = self._hide_step(x, min(t_end, hi)) + 1
            else:
                job[0] = self._show_step(x, min(h_end, hi)) + 1
            return False
        return True

    def _run(self, budget: float) -> None:
        """Run insertion / folding steps for ``budget`` seconds; the rest on the timer."""
        t0 = time.perf_counter()
        before = self.document().blockCount()
        first = self.firstVisibleBlock().blockNumber()
        worked = False
        while True:
            if self._rebuild_due:
                self._rebuild_start()             # cheap: the first chunk follows in this step
                continue
            if self._jobs:
                if self._converge_step(self._jobs[0]):
                    self._jobs.pop(0)
            elif self._loaded < self._total:
                self._load_chunk()
            else:
                break
            worked = True
            if time.perf_counter() - t0 >= budget:
                break
        if worked:
            self.gutter.update()
            if before <= first + 400 or self._jobs:
                self.schedule_extras()
            self._report_progress()
        self._try_goal()
        if self.busy():
            if not self._work_timer.isActive():
                self._work_timer.start()
        else:
            self._work_timer.stop()
        if self._just_loaded:
            self._just_loaded = False
            self.content_loaded.emit()

    def _work_tick(self) -> None:
        self._run(TICK_BUDGET_S)

    def _report_progress(self) -> None:
        if self._loaded >= self._total:
            if self._progress != 100:
                self._progress = 100
                self._view.loading_progress(1.0)
            return
        pct = int(self._loaded * 100 / self._total)
        if pct != self._progress:
            self._progress = pct
            self._view.loading_progress(self._loaded / self._total)

    # ------------------------------------------------------------------ goals
    def set_goal(self, *, top: int | None = None, caret: int | None = None,
                 select: tuple[int, int, int] | None = None) -> None:
        """Show a position as soon as its lines are in the document (now when they are)."""
        self._goal = _Goal(top, caret, select)
        self._try_goal()

    def goal_pending(self) -> bool:
        return self._goal is not None

    def position(self) -> tuple[int, int]:
        """``(first shown line, caret line)`` in source lines: of the pending goal while it
        waits for its lines (a position being restored), else of the screen."""
        g = self._goal
        if g is not None:
            sel = g.select[0] if g.select is not None else None
            caret = g.caret if g.caret is not None else sel
            top = g.top if g.top is not None else caret
            if top is not None:
                return top, caret if caret is not None else top
        return self.line_of_block(self.firstVisibleBlock().blockNumber()), self.current_line()

    def _shown_ancestor(self, line: int) -> int:
        """``line``, or the opening line of the folded region hiding it."""
        k = self._target.find(line)
        return self._target.a[k] - 1 if k >= 0 else line

    def _try_goal(self) -> None:
        g = self._goal
        if g is None or self._jobs or self._rebuild_due:
            return
        wanted = [x for x in (g.top, g.caret, g.select[0] if g.select else None) if x is not None]
        if any(self._shown_ancestor(min(x, self._total - 1)) >= self._loaded for x in wanted):
            return
        self._goal = None
        doc = self.document()
        if g.top is not None:
            n = self.block_of_line(self._shown_ancestor(min(g.top, self._total - 1)))
            if n is not None:
                bar = self.verticalScrollBar()
                bar.setValue(bar.maximum())       # moving the caret up from the end puts it at the top
                self.setTextCursor(QTextCursor(doc.findBlockByNumber(n)))
                self.ensureCursorVisible()
        if g.select is not None:
            line, col, length = g.select
            n = self.block_of_line(line)
            if n is None:
                n = self.block_of_line(self._shown_ancestor(min(line, self._total - 1)))
                col = length = 0
            if n is None:
                return
            block = doc.findBlockByNumber(n)
            text = block.text()
            cursor = QTextCursor(block)
            cursor.setPosition(block.position() + _u16(text, min(col, len(text))))
            cursor.setPosition(block.position() + _u16(text, min(col + length, len(text))),
                               QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cursor)
            self.ensureCursorVisible()
        elif g.caret is not None:
            n = self.block_of_line(self._shown_ancestor(min(g.caret, self._total - 1)))
            if n is not None:
                self.setTextCursor(QTextCursor(doc.findBlockByNumber(n)))
        self.schedule_extras()

    # ------------------------------------------------------------------ colors
    def refresh_colors(self) -> None:
        """Theme changed: blocks get new formats when painted next."""
        self._version += 1
        self._formats = {}
        self._colors_cache = {}
        self.viewport().update()
        self.gutter.update()
        self.schedule_extras()

    def _color(self, role: str) -> QColor:
        c = self._colors_cache.get(role)
        if c is None:
            c = QColor(theme.color(role))
            self._colors_cache[role] = c
        return c

    def _format(self, role: str) -> QTextCharFormat:
        fmt = self._formats.get(role)
        if fmt is None:
            fmt = QTextCharFormat()
            fmt.setForeground(self._color({"punct": "muted"}.get(role, role)))
            self._formats[role] = fmt
        return fmt

    def _highlight_block(self, block: QTextBlock) -> None:
        text = block.text()
        ranges = []
        astral = _ASTRAL_RE.search(text) is not None
        for s, e, role in line_tokens(text):
            r = QTextLayout.FormatRange()
            if astral:
                us, ue = _u16(text, s), _u16(text, e)
            else:
                us, ue = s, e
            r.start = us
            r.length = ue - us
            r.format = self._format(role)
            ranges.append(r)
        block.layout().setFormats(ranges)
        block.setUserState(self._version)
        self.document().markContentsDirty(block.position(), block.length())

    def visible_blocks(self) -> Iterator[tuple[QTextBlock, float, float, int]]:
        """``(block, top, height, source line)`` of the blocks in the viewport (walked from
        the first visible block with their geometry; never a hit test)."""
        block = self.firstVisibleBlock()
        if not block.isValid():
            return
        offset = self.contentOffset()
        top = self.blockBoundingGeometry(block).translated(offset).top()
        bottom = self.viewport().height()
        hidden = self._hidden
        line = hidden.to_source(block.blockNumber())
        while block.isValid() and top <= bottom:
            h = self.blockBoundingRect(block).height()
            yield block, top, h, line
            top += h
            block = block.next()
            line = hidden.next_free(line)

    def visible_line_range(self) -> tuple[int, int]:
        """First and last source line in the viewport."""
        first = last = None
        for _block, _top, _h, line in self.visible_blocks():
            if first is None:
                first = line
            last = line
        if first is None:
            return 0, 0
        return first, last

    def is_empty(self) -> bool:
        return self.document().characterCount() <= 1

    def set_empty_text(self, text: str) -> None:
        """Muted text shown in the middle while the editor is empty (no node / loading)."""
        self._empty_text = text or ""
        self.viewport().update()
        self.gutter.update()
        self.schedule_extras()

    def empty_text(self) -> str:
        return self._empty_text

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt name)
        if self.is_empty():
            super().paintEvent(event)
            if self._empty_text:
                p = QPainter(self.viewport())
                try:
                    p.setPen(self._color("faint"))
                    rect = self.viewport().rect().adjusted(16, 16, -16, -16)
                    p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap), self._empty_text)
                finally:
                    p.end()
            return
        version = self._version
        for block, _top, _h, _line in list(self.visible_blocks()):
            if block.userState() != version:
                self._highlight_block(block)
        super().paintEvent(event)
        self._paint_placeholders()

    def _paint_placeholders(self) -> None:
        self._placeholders = []
        if not self.folded:
            return
        p = QPainter(self.viewport())
        try:
            fm = self.fontMetrics()
            margin = self.document().documentMargin()
            x0 = self.contentOffset().x() + margin
            muted = self._color("muted")
            border = self._color("line")
            for block, top, _h, n in self.visible_blocks():
                if n not in self.folded or self._hidden.find(n + 1) < 0:
                    continue
                close = self.folds.close_of(n)
                if close is None:
                    continue
                layout = block.layout()
                if layout is None or layout.lineCount() == 0:
                    continue
                line = layout.lineAt(layout.lineCount() - 1)
                label = f" {ELLIPSIS} {self.line_text(close).strip()} "
                w = fm.horizontalAdvance(label)
                x = x0 + line.naturalTextWidth() + fm.horizontalAdvance(" ")
                y = top + line.y()
                rect = QRectF(x, y + 1, w, line.height() - 2)
                p.setPen(border)
                p.drawRoundedRect(rect, 3, 3)
                p.setPen(muted)
                p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), label)
                self._placeholders.append((rect, n))
        finally:
            p.end()

    # ------------------------------------------------------------------ gutter
    def gutter_width(self) -> int:
        fm = self.fontMetrics()
        digits = max(3, len(str(max(1, self._total))))
        return fm.horizontalAdvance("9") * digits + fm.horizontalAdvance("▸") + fm.horizontalAdvance(" ") * 2 + 8

    def _fold_column(self) -> tuple[int, int]:
        fm = self.fontMetrics()
        w = self.gutter_width()
        fw = fm.horizontalAdvance("▸") + fm.horizontalAdvance(" ") + 4
        return w - fw, fw

    def _update_gutter_width(self) -> None:
        w = self.gutter_width()
        if w != self._gutter_w:
            self._gutter_w = w
            self.setViewportMargins(w, 0, 0, 0)
        cr = self.contentsRect()
        rect = QRect(cr.left(), cr.top(), w, cr.height())
        if self.gutter.geometry() != rect:
            self.gutter.setGeometry(rect)
        self.gutter.update()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().resizeEvent(event)
        self._update_gutter_width()
        self.schedule_extras()

    def _on_update_request(self, rect: QRect, dy: int) -> None:
        if dy:
            self.gutter.scroll(0, dy)
        self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())

    def paint_gutter(self, widget: QWidget, event: QPaintEvent) -> None:
        p = QPainter(widget)
        try:
            pal = self.palette()
            base = pal.color(QPalette.ColorRole.Base)
            window = pal.color(QPalette.ColorRole.Window)
            bg = QColor(round(base.red() * 0.3 + window.red() * 0.7), round(base.green() * 0.3 + window.green() * 0.7),
                        round(base.blue() * 0.3 + window.blue() * 0.7))
            p.fillRect(event.rect(), bg)
            if self.is_empty():
                return
            p.setFont(self.font())
            muted = self._color("faint")
            fg = pal.color(QPalette.ColorRole.Text)
            accent = self._color("accent")
            fx, fw = self._fold_column()
            current = self.current_line()
            fm = self.fontMetrics()
            for _block, top, _h, n in self.visible_blocks():
                rect = QRect(0, round(top), fx - 4, fm.height())
                p.setPen(fg if n == current else muted)
                p.drawText(rect, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), str(n + 1))
                if self.folds.close_of(n) is not None:
                    folded = n in self.folded
                    p.setPen(accent if folded else muted)
                    p.drawText(QRect(fx, round(top), fw, fm.height()), int(Qt.AlignmentFlag.AlignCenter),
                               "▸" if folded else "▾")
            p.setPen(self._color("line"))
            p.drawLine(widget.width() - 1, event.rect().top(), widget.width() - 1, event.rect().bottom())
        finally:
            p.end()

    def gutter_clicked(self, pos: QPoint) -> None:
        cursor = self.cursorForPosition(QPoint(0, pos.y()))
        n = self.line_of_block(cursor.blockNumber())
        fx, _fw = self._fold_column()
        if pos.x() >= fx - 2 and self.folds.close_of(n) is not None:
            self.toggle_fold(n)

    # ------------------------------------------------------------------ folding
    def is_folded(self, line: int) -> bool:
        return line in self.folded

    def can_fold(self, line: int) -> bool:
        return self.folds.close_of(line) is not None

    def toggle_fold(self, line: int) -> bool:
        if line in self.folded:
            return self.unfold(line)
        return self.fold(line)

    def _closed_folds(self, lines: Any) -> set[int]:
        """The lines of ``lines`` that open a closed region (C-level set operations)."""
        fm = self.folds
        return set(lines) & set(compress(fm.opens, map(gt, fm.closes, fm.opens)))

    def _outer_regions(self, i0: int = 0, i1: int | None = None) -> tuple[list[int], list[int]]:
        """``(starts, ends)`` of the line ranges hidden by the folded regions among regions
        ``i0..i1-1`` (outermost only; every folded line opens a closed region)."""
        fm = self.folds
        opens, closes = fm.opens, fm.closes
        if i1 is None:
            i1 = len(opens)
        a: list[int] = []
        b: list[int] = []
        end = -1
        for i in compress(range(i0, i1), map(self.folded.__contains__, islice(opens, i0, i1))):
            o = opens[i]
            if o > end:
                end = closes[i]
                a.append(o + 1)
                b.append(end)
        return a, b

    def _converge(self, lo: int, hi: int) -> None:
        if hi >= lo:
            self._jobs.append([lo, hi])
        self._run(SYNC_BUDGET_S)

    def _folds_touched(self) -> None:
        self.viewport().update()
        self.gutter.update()
        self.schedule_extras()
        self.folds_changed.emit()

    def fold(self, line: int) -> bool:
        """Hide the lines inside the region opened at ``line`` (closing line included)."""
        if line in self.folded or not self.can_fold(line):
            return False
        close = self.folds.close_of(line)
        assert close is not None
        self.folded.add(line)
        if self._target.find(line) < 0:          # not inside a folded region
            self._target.replace(line + 1, close, [line + 1], [close])
            self._converge(line + 1, close)
        self._folds_touched()
        return True

    def unfold(self, line: int) -> bool:
        if line not in self.folded:
            return False
        self.folded.discard(line)
        close = self.folds.close_of(line)
        if close is not None and self._target.find(line) < 0:
            fm = self.folds
            i = fm.index(line)
            self._target.replace(line + 1, close, *self._outer_regions(i + 1, bisect.bisect_right(fm.opens, close)))
            self._converge(line + 1, close)
        self._folds_touched()
        return True

    def _apply_folded(self, folded: set[int], ranges: LineRanges | tuple[list[int], list[int]], *,
                      started: float | None = None) -> None:
        """Make ``folded`` the folded set and ``ranges`` the hidden lines. The state changes
        at once (later folds, unfolds and goals build on it); the document follows in
        bounded steps -- from the next event-loop turn on when computing the change took a
        while (``started``: when that began)."""
        self.folded = folded
        self._target = ranges if isinstance(ranges, LineRanges) else LineRanges.from_lists(*ranges)
        rebuild = len(self._target) + len(self._hidden) > REBUILD_RANGES and self._loaded > 0
        if rebuild:
            self._jobs = []
            self._rebuild_due = True
        else:
            self._jobs = [[0, self._total - 1]]
        if started is not None and time.perf_counter() - started > DEFER_AFTER_S:
            self._work_timer.start()              # the document changes on the next turn
        else:
            budget = SYNC_BUDGET_S if started is None else max(0.0, SYNC_BUDGET_S - (time.perf_counter() - started))
            self._run(budget)
        self._folds_touched()

    def _rebuild_start(self) -> None:
        """Insert the shown lines again from the start (bulk fold changes); the current
        position comes back once it is inserted."""
        self._rebuild_due = False
        goal = self._goal
        if goal is None:
            top = self.line_of_block(self.firstVisibleBlock().blockNumber())
            goal = _Goal(top=top, caret=self.current_line())
        self._jobs = []
        self._hidden = LineRanges()
        self._loaded = 0
        self._placeholders = []
        self._progress = -1
        self._goal = goal
        self._fresh_document()

    def set_folded(self, lines: set[int]) -> None:
        """Replace the folded set (restoring folds, unfold all)."""
        t0 = time.perf_counter()
        self.folded = self._closed_folds(lines)
        self._apply_folded(self.folded, self._outer_regions(), started=t0)

    def fold_to_level(self, level: int) -> None:
        """Fold every region nested ``level`` or deeper (1: the outermost container stays
        open and its members are folded)."""
        t0 = time.perf_counter()
        fm = self.folds
        opens, closes, depths = fm.opens, fm.closes, fm.depths
        n = len(opens)
        closed = list(map(gt, closes, opens))
        folded = set(compress(opens, map(and_, closed, map(ge, depths, repeat(level)))))
        at_level = list(map(eq, depths, repeat(level)))
        sel = list(map(and_, at_level, closed))
        # the closed regions of the level hold every deeper region, except below an unclosed
        # one (a cut text): every region after that one lies inside it
        a = list(map(add, compress(opens, sel), repeat(1)))
        b = list(compress(closes, sel))
        unclosed = next(compress(range(n), map(and_, at_level, map(not_, closed))), None)
        self.folded = folded
        if unclosed is not None:
            ta, tb = self._outer_regions(unclosed + 1, n)
            a.extend(ta)
            b.extend(tb)
        self._apply_folded(folded, (a, b), started=t0)

    def fold_all(self) -> None:
        self.fold_to_level(1)

    def unfold_all(self) -> None:
        self._apply_folded(set(), LineRanges())

    def reveal_line(self, line: int) -> None:
        """Unfold the folded regions hiding ``line``."""
        if self._target.find(line) < 0:
            return
        fm = self.folds
        hiding = []
        r = bisect.bisect_left(fm.opens, line) - 1          # the regions holding the line are its ancestors
        while r >= 0:
            if fm.closes[r] >= line and fm.opens[r] in self.folded:
                hiding.append(fm.opens[r])
            r = fm.parents[r]
        for o in reversed(hiding):                           # the outermost first
            self.unfold(o)

    def placeholder_rects(self) -> list[tuple[QRectF, int]]:
        return list(self._placeholders)

    # ------------------------------------------------------------------ extra selections
    def schedule_extras(self) -> None:
        if not self._extras_due:
            self._extras_due = True
            QTimer.singleShot(0, self, self.update_extras)

    def update_extras(self) -> None:
        self._extras_due = False
        if self.is_empty():
            self.setExtraSelections([])
            return
        sels: list[QTextEdit.ExtraSelection] = []
        cur = QTextEdit.ExtraSelection()
        fmt = QTextCharFormat()
        fmt.setBackground(self._color("hover"))
        fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
        cur.format = fmt
        c = QTextCursor(self.textCursor())
        c.clearSelection()
        cur.cursor = c
        sels.append(cur)
        sels.extend(self._view.match_selections(*self.visible_line_range()))
        sels.extend(self._bracket_selections())
        self.setExtraSelections(sels)

    def _bracket_selections(self) -> list[QTextEdit.ExtraSelection]:
        cursor = self.textCursor()
        if cursor.hasSelection():
            return []
        block = cursor.block()
        text = block.text()
        n = self.line_of_block(block.blockNumber())
        col = _cp(text, cursor.positionInBlock())
        pairs: list[tuple[int, int, int, int]] = []   # (line, col, line, col): source lines
        for ci in (col, col - 1):
            if not 0 <= ci < len(text):
                continue
            ch = text[ci]
            if ch in "{[":
                if ci + 1 < len(text) and text[ci + 1] in "}]":
                    pairs.append((n, ci, n, ci + 1))
                elif ci == len(text) - 1:
                    close = self.folds.close_of(n)
                    if close is not None:
                        ct = self.line_text(close)
                        cc = len(ct) - len(ct.lstrip(" \t"))
                        pairs.append((n, ci, close, cc))
            elif ch in "}]":
                if ci > 0 and text[ci - 1] in "{[":
                    pairs.append((n, ci - 1, n, ci))
                elif text[:ci].strip(" \t") == "":
                    o = self.folds.open_of(n)
                    if o is not None:
                        ot = self.line_text(o)
                        pairs.append((o, len(ot.rstrip()) - 1, n, ci))
            if pairs:
                break
        out = []
        if pairs:
            fmt = QTextCharFormat()
            fmt.setBackground(self._color("accent_soft"))
            fmt.setForeground(self._color("accent"))
            l1, c1, l2, c2 = pairs[0]
            for line, ci in ((l1, c1), (l2, c2)):
                b = self.block_for_line(line)
                if not b.isValid() or ci < 0:
                    continue
                bt = b.text()
                sel = QTextEdit.ExtraSelection()
                cc = QTextCursor(b)
                cc.setPosition(b.position() + _u16(bt, ci))
                cc.setPosition(b.position() + _u16(bt, ci + 1), QTextCursor.MoveMode.KeepAnchor)
                sel.cursor = cc
                sel.format = fmt
                out.append(sel)
        return out

    # ------------------------------------------------------------------ copy
    def createMimeDataFromSelection(self) -> QMimeData:  # noqa: N802 (Qt name)
        """Copy / drag: the complete text of the selection (not the display text)."""
        text = self._view.selected_full_text()
        if text is None:
            return super().createMimeDataFromSelection()
        return qt_mime_data(text)

    # ------------------------------------------------------------------ input
    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 (Qt name)
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self._view.zoom(1 if delta > 0 else -1)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt name)
        before = self.textCursor().position()
        super().keyPressEvent(event)
        if self.textCursor().position() != before:
            self.caret_moved.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            for rect, line in self._placeholders:
                if rect.contains(pos):
                    self.unfold(line)
                    event.accept()
                    return
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.caret_moved.emit()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 (Qt name)
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.caret_moved.emit()

    def contextMenuEvent(self, event: QContextMenuEvent) -> None:  # noqa: N802 (Qt name)
        menu = self._view.build_context_menu()
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(event.globalPos())

    def stop(self) -> None:
        """No further steps (the view is closing)."""
        self._work_timer.stop()
        self._jobs = []
        self._rebuild_due = False
        self._goal = None
        self._retire_timer.stop()
        for doc in self._retired:
            doc.deleteLater()
        self._retired = []

    # ------------------------------------------------------------------ documents
    def _fresh_document(self) -> None:
        """An empty document for new content. A big old document is not cleared at once
        (Qt frees its blocks in one go, hundreds of ms for a million lines): a new
        document takes its place and the old one is emptied in slices, then deleted."""
        old = self.document()
        if old.characterCount() <= 1:
            return
        self.setExtraSelections([])
        if old.blockCount() <= RETIRE_BLOCKS:
            self.setPlainText("")
            return
        new = QTextDocument(self)
        new.setDocumentLayout(QPlainTextDocumentLayout(new))
        new.setUndoRedoEnabled(False)
        new.setDefaultFont(old.defaultFont())
        new.setDefaultTextOption(old.defaultTextOption())
        new.setDocumentMargin(old.documentMargin())
        old.setParent(self)               # not the text control's child: setDocument() keeps it
        self.setDocument(new)
        self.setLineWrapMode(self.lineWrapMode())
        self._retired.append(old)
        if not self._retire_timer.isActive():
            self._retire_timer.start()

    def _retire_tick(self) -> None:
        """Empty the replaced documents slice by slice (from the end), then delete them."""
        t0 = time.perf_counter()
        while self._retired and time.perf_counter() - t0 < TICK_BUDGET_S:
            doc = self._retired[0]
            n = doc.blockCount()
            if n <= RETIRE_BLOCKS:
                doc.clear()
                self._retired.pop(0)
                doc.deleteLater()
                continue
            block = doc.findBlockByNumber(n - RETIRE_BLOCKS)
            cursor = QTextCursor(doc)
            cursor.setPosition(block.position() - 1)
            cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        if not self._retired:
            self._retire_timer.stop()


# ==================================================================================
# pretty texts shared by the text views of a session
# ==================================================================================
class _Build:
    """A pretty text being formatted and the views waiting for it (``view -> generation``)."""

    __slots__ = ("task", "waiters")

    def __init__(self) -> None:
        self.task: Any = None
        self.waiters: dict[Any, int] = {}


class _SharedTexts:
    """The last :data:`PRETTY_CACHE` pretty texts of one session's document and the ones
    being formatted: a node shown in two panes (or shown again) is formatted once."""

    def __init__(self) -> None:
        self.doc_ref: Any = None
        self.entries: list[tuple[tuple, PrettyText]] = []     # newest last
        self.pending: dict[tuple, _Build] = {}

    def get(self, key: tuple) -> PrettyText | None:
        for i, (k, r) in enumerate(self.entries):
            if k == key:
                self.entries.append(self.entries.pop(i))
                return r
        return None

    def put(self, key: tuple, result: PrettyText) -> None:
        self.entries = [e for e in self.entries if e[0] != key]
        self.entries.append((key, result))
        del self.entries[:-PRETTY_CACHE]


_SHARED: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _shared_texts(session: Any) -> _SharedTexts:
    """The shared texts of ``session`` (emptied when it shows another Document)."""
    st = _SHARED.get(session)
    if st is None:
        st = _SharedTexts()
        _SHARED[session] = st
    doc = getattr(session, "doc", None)
    if (st.doc_ref() if st.doc_ref is not None else None) is not doc:
        for build in st.pending.values():
            if build.task is not None:
                build.task.cancel()
        st.doc_ref = weakref.ref(doc) if doc is not None else None
        st.entries = []
        st.pending = {}
    return st


# ==================================================================================
# the view
# ==================================================================================
class TextView(ViewBase):
    """Pretty JSON of the current node (see the module docstring)."""

    view_id = "text"
    #: ``(offset, length)`` in the data file: the document tab shows them in a raw view.
    #: Python ints (offsets may exceed 2**31).
    open_raw_requested = Signal(object, object)
    #: A node's text (or its notice) is shown -- emitted after the first chunk.
    loaded = Signal()
    #: The editor's text selection changed (status bar / search prefill).
    selection_changed = Signal()

    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(session, parent)
        self.setObjectName("TextView")
        self._ref: Any = None              # the node shown (or being loaded)
        self._pending: Any = getattr(session, "current", None)
        self._dirty = True
        self._build: tuple[tuple, _Build] | None = None     # the pretty text this view waits for
        self._gen = 0
        self._result: PrettyText | None = None
        self._error: BaseException | None = None
        self._matches = Matches()
        self._match_cur: int | None = None
        self._find_task: Any = None
        self._find_gen = 0
        self._restore: tuple[int, int, set[int], int, bool] | None = None   # indent change: position, folds
        self._zoom = 0.0
        self._dead = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(4, 2, 4, 0)
        top.setSpacing(6)
        self.path_label = ElidedLabel("", self)
        self.path_label.setObjectName("text_path")
        top.addWidget(self.path_label, 1)
        self.size_label = QLabel("", self)
        self.size_label.setObjectName("text_size")
        self.size_label.setEnabled(False)       # muted
        top.addWidget(self.size_label)
        top.addWidget(QLabel(t("gui.text.indent"), self))
        self.indent_combo = QComboBox(self)
        self.indent_combo.setObjectName("text_indent")
        self.indent_combo.setToolTip(t("gui.text.indent.tip"))
        for key, _value in INDENTS:
            self.indent_combo.addItem(t("gui.text.indent.tab") if key == "tab" else key, key)
        top.addWidget(self.indent_combo)
        self.wrap_button = QToolButton(self)
        self.wrap_button.setObjectName("text_wrap")
        self.wrap_button.setText(t("gui.text.wrap"))
        self.wrap_button.setToolTip(t("gui.text.wrap.tip"))
        self.wrap_button.setCheckable(True)
        top.addWidget(self.wrap_button)
        self.fold_button = QToolButton(self)
        self.fold_button.setObjectName("text_fold")
        self.fold_button.setText(t("gui.text.fold"))
        self.fold_button.setToolTip(t("gui.text.fold.tip"))
        self.fold_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.fold_menu = QMenu(self.fold_button)
        self._fill_fold_menu(self.fold_menu)
        self.fold_button.setMenu(self.fold_menu)
        top.addWidget(self.fold_button)
        self.copy_button = QToolButton(self)
        self.copy_button.setObjectName("text_copy")
        self.copy_button.setText(t("gui.common.copy"))
        self.copy_button.setToolTip(t("gui.text.copy.tip"))
        self.copy_button.clicked.connect(lambda _c=False: self.copy_text())
        top.addWidget(self.copy_button)
        layout.addLayout(top)

        self.info_bar = InfoBar("info", "", parent=self)
        self.info_bar.setVisible(False)
        layout.addWidget(self.info_bar)

        self.editor = CodeEditor(self)
        layout.addWidget(self.editor, 1)
        self.find_bar = QuickFindBar(self)
        layout.addWidget(self.find_bar)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(4, 0, 4, 2)
        self.status_label = QLabel("", self)
        self.status_label.setObjectName("text_status")
        self.status_label.setEnabled(False)
        bottom.addWidget(self.status_label, 1)
        layout.addLayout(bottom)

        self._caret_timer = QTimer(self)
        self._caret_timer.setSingleShot(True)
        self._caret_timer.setInterval(CARET_DEBOUNCE_MS)
        self._caret_timer.timeout.connect(self._select_at_caret)

        # remembered options
        state = getattr(session.app, "state", None)
        indent_key = str(state.get(STATE_INDENT, "2")) if state is not None else "2"
        idx = self.indent_combo.findData(indent_key)
        self.indent_combo.setCurrentIndex(idx if idx >= 0 else 0)
        wrap = bool(state.get(STATE_WRAP, False)) if state is not None else False
        self.wrap_button.setChecked(wrap)
        self._apply_wrap(wrap)
        self.indent_combo.currentIndexChanged.connect(self._indent_changed)
        self.wrap_button.toggled.connect(self.set_wrap)

        font = mono_font(session)
        self._base_font = QFont(font)
        self._base_size = font.pointSizeF() if font.pointSizeF() > 0 else 10.0
        self._apply_font(font)

        self.editor.cursorPositionChanged.connect(self._update_status)
        self.editor.selectionChanged.connect(self.selection_changed)
        self.editor.content_loaded.connect(self._content_loaded)
        self.editor.folds_changed.connect(self._update_fold_actions)
        self.editor.caret_moved.connect(self._caret_moved)
        self.find_bar.search_changed.connect(self._on_find_changed)
        self.find_bar.next_requested.connect(lambda: self.find_next(False))
        self.find_bar.prev_requested.connect(lambda: self.find_next(True))
        self.find_bar.closed.connect(lambda: self.editor.setFocus())

        session.current_changed.connect(self._on_current_changed)
        session.opened.connect(self._on_doc_opened)
        settings_changed = getattr(session.app, "settings_changed", None)
        if settings_changed is not None:
            settings_changed.connect(self._on_settings_changed)
        self._show_empty()
        self._update_fold_actions()

    # ------------------------------------------------------------------ options
    def indent(self) -> int | str:
        key = self.indent_combo.currentData()
        return dict(INDENTS).get(key, 2)

    def set_indent(self, value: int | str) -> None:
        key = "tab" if value == "\t" or value == "tab" else str(value)
        idx = self.indent_combo.findData(key)
        if idx >= 0:
            self.indent_combo.setCurrentIndex(idx)

    def _indent_changed(self, _index: int) -> None:
        key = self.indent_combo.currentData()
        self._remember(STATE_INDENT, key)
        ed = self.editor
        r = self._result
        if r is not None and self._ref is not None and not r.virtual:
            # a position still being restored (its lines not inserted yet) is kept as it is;
            # while the text is formatted again (r None) the saved one stays
            first, caret = ed.position()
            self._restore = (first, caret, set(ed.folded), ed.line_count(), r.truncated)
        self._start_load(self._ref)

    def wrap(self) -> bool:
        return self.wrap_button.isChecked()

    def set_wrap(self, on: bool) -> None:
        if self.wrap_button.isChecked() != bool(on):
            self.wrap_button.setChecked(bool(on))
            return
        self._apply_wrap(bool(on))
        self._remember(STATE_WRAP, bool(on))

    def _apply_wrap(self, on: bool) -> None:
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth if on
                                    else QPlainTextEdit.LineWrapMode.NoWrap)

    def _remember(self, key: str, value: Any) -> None:
        state = getattr(self.session.app, "state", None)
        if state is None:
            return
        try:
            state.set(key, value)
        except (TypeError, ValueError):
            log.debug("could not remember %s", key, exc_info=True)

    def _apply_font(self, font: QFont) -> None:
        self.editor.setFont(font)
        self.editor.document().setDefaultFont(font)
        self.editor.setTabStopDistance(self.editor.fontMetrics().horizontalAdvance(" ") * 4)
        self.editor.gutter.setFont(font)
        self.editor._update_gutter_width()

    def zoom(self, steps: int) -> None:
        if steps == 0:
            self._zoom = 0.0
        else:
            size = min(_MAX_POINT, max(_MIN_POINT, self._base_size + self._zoom + steps))
            self._zoom = size - self._base_size
        self._apply_zoomed()

    def _apply_zoomed(self) -> None:
        font = QFont(self._base_font)
        font.setPointSizeF(min(_MAX_POINT, max(_MIN_POINT, self._base_size + self._zoom)))
        self._apply_font(font)

    def _on_settings_changed(self) -> None:
        """The data font may have changed: take it, keeping the zoom steps."""
        if self._dead:
            return
        font = mono_font(self.session)
        self._base_font = QFont(font)
        self._base_size = font.pointSizeF() if font.pointSizeF() > 0 else 10.0
        self._apply_zoomed()

    def font_point_size(self) -> float:
        return self.editor.font().pointSizeF()

    # ------------------------------------------------------------------ following the session
    def _on_current_changed(self, ref: Any, origin: Any) -> None:
        if origin is self:
            return
        self._pending = ref
        self._dirty = True
        if self.isVisible():
            self._load_pending()

    def _on_doc_opened(self) -> None:
        self._pending = getattr(self.session, "current", None)
        self._dirty = True
        self._ref = None
        if self.isVisible():
            self._load_pending()

    def activate(self) -> None:
        """The view became visible: load the node selected meanwhile."""
        self._load_pending()

    def showEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        super().showEvent(event)
        if self._dirty:
            QTimer.singleShot(0, self, self._load_pending)

    def _load_pending(self) -> None:
        if not self._dirty or self._dead:
            return
        self._dirty = False
        ref = self._pending
        if ref is not None and self._ref is not None and _same(ref, self._ref) and \
                (self._build is not None or self._result is not None):
            return
        self._restore = None
        self._start_load(ref)

    def shown_ref(self) -> Any:
        """The node whose text is shown / being loaded."""
        return self._ref

    def current_ref(self) -> Any:
        return self._ref if self._ref is not None else super().current_ref()

    # ------------------------------------------------------------------ the node at the caret
    def _caret_moved(self) -> None:
        if self._result is not None and not self._result.virtual:
            self._caret_timer.start()

    def ref_at_line(self, line: int) -> Any:
        """The node written on source ``line`` (a NodeRef, ``None``: unknown)."""
        r, ref = self._result, self._ref
        if r is None or r.virtual or ref is None:
            return None
        found = node_path_at(r, line)
        if found is None:
            return None
        rel, idx = found
        path = tuple(ref.path) + rel
        base = r.ipath
        if base is None and ref.ipath is not None and len(ref.ipath) == len(ref.path):
            base = tuple(ref.ipath)
        # the child indices address the exact member, also among duplicate keys
        ipath = tuple(base) + idx if base is not None else None
        return type(ref)(ref.record, path, ipath)

    def _select_at_caret(self) -> None:
        if self._dead:
            return
        new = self.ref_at_line(self.editor.current_line())
        if new is None:
            return
        try:
            self.session.select(new, origin=self)
        except JsonReshapeError as exc:
            log.debug("text view: selecting %r failed: %r", new, exc)

    # ------------------------------------------------------------------ loading
    def _cache_key(self, ref: Any, indent: int | str) -> tuple:
        return (ref.record, tuple(ref.path), _identity(ref), str(indent))

    def _leave_build(self) -> None:
        """Stop waiting for a pretty text (cancelled when no other view waits for it)."""
        if self._build is None:
            return
        key, build = self._build
        self._build = None
        build.waiters.pop(self, None)
        if not build.waiters:
            # nobody waits: cancel it and forget it at once (the task holds the session; a
            # forgotten entry must not keep it alive when the tab closes meanwhile)
            st = _SHARED.get(self.session)
            if st is not None and st.pending.get(key) is build:
                del st.pending[key]
            if build.task is not None:
                build.task.cancel()

    def _start_load(self, ref: Any) -> None:
        self._leave_build()
        self._caret_timer.stop()
        self._gen += 1
        self._ref = ref
        self._result = None
        self._error = None
        self._clear_find()
        self.info_bar.setVisible(False)
        if ref is None or getattr(self.session, "doc", None) is None:
            self._show_empty()
            return
        self.path_label.setText(self._location(ref))
        self.size_label.setText("")
        session, indent, gen = self.session, self.indent(), self._gen
        key = self._cache_key(ref, indent)
        st = _shared_texts(session)
        cached = st.get(key)
        if cached is not None:
            self._loaded(gen, cached)
            return
        self.editor.clear_content()
        self.editor.set_empty_text(t("gui.common.loading"))
        self.status_label.setText(t("gui.common.loading"))
        build = st.pending.get(key)
        if build is None:
            build = st.pending[key] = _Build()

            session_ref = weakref.ref(session)

            def work(ctx: Any) -> PrettyText:
                s = session_ref()
                if s is None:
                    raise Cancelled()
                return build_pretty(s, ref, indent, ctx.cancel, cap=TEXT_CAP, max_line=MAX_LINE_CHARS)

            def finished() -> list[tuple[Any, int]]:
                if st.pending.get(key) is build:
                    del st.pending[key]
                waiters = list(build.waiters.items())
                build.waiters.clear()
                return waiters

            def done(result: PrettyText) -> None:
                waiters = finished()
                if not result.virtual:
                    st.put(key, result)
                for view, g in waiters:
                    view._loaded(g, result)

            def failed(exc: BaseException) -> None:
                for view, g in finished():
                    view._failed(g, exc)

            def cancelled() -> None:
                for view, g in finished():
                    view._cancelled(g)

            # owned by the session: a view leaving a shared build does not cancel it for the others
            build.task = session.app.tasks.run(work, title=t("gui.text.task.format"), kind="text", owner=session,
                                               silent=True, on_done=done, on_error=failed, on_cancel=cancelled)
        build.waiters[self] = gen
        self._build = (key, build)

    def _location(self, ref: Any) -> str:
        try:
            return self.session.location_text(ref)
        except Exception:
            log.debug("location_text failed", exc_info=True)
            return "?"

    def _cancelled(self, gen: int) -> None:
        if gen == self._gen:
            self._build = None

    def _failed(self, gen: int, exc: BaseException) -> None:
        if gen != self._gen:
            return
        self._build = None
        self._error = exc
        self.editor.clear_content()
        self.editor.set_empty_text("")
        self.size_label.setText("")
        self.status_label.setText("")
        message = exc.message if isinstance(exc, JsonReshapeError) else str(exc) or type(exc).__name__
        self._show_bar("error", t("gui.text.load_failed", message=message))
        if not isinstance(exc, JsonReshapeError):
            log.error("text view: loading %r failed", self._ref, exc_info=(type(exc), exc, exc.__traceback__))
        self.loaded.emit()

    def _loaded(self, gen: int, result: PrettyText) -> None:
        if gen != self._gen:
            return
        self._build = None
        self._result = result
        self.editor.set_empty_text("")
        ref = self._ref
        if result.virtual:
            self.editor.clear_content()
            self.size_label.setText(format_size(result.size))
            self.status_label.setText("")
            self._show_bar("info", t("gui.text.virtual", count=format_count(result.records)),
                           [(t("gui.text.open_raw"), lambda: self.request_raw())])
            self.loaded.emit()
            return
        self.path_label.setText(self._location(ref))
        kind_key = f"gui.text.kind.{result.kind}"
        kind = t(kind_key)
        self.size_label.setText(t("gui.text.size", kind=result.kind if kind == kind_key else kind,
                                  size=format_size(result.size)))
        self._show_notices(result)
        restore = self._restore
        self._restore = None
        starts = result.line_starts()
        # the lines of the same node are the same tokens with any indent; a text cut at the
        # size limit ends at another line (its folds / position are taken where they exist)
        if restore is not None and (restore[3] == len(starts) - 1 or restore[4] or result.truncated):
            first, caret, folded, _n, _cut = restore
            self.editor.set_content(result.text, result.folds, starts=starts, folded=folded,
                                    goal=_Goal(top=first, caret=caret))
        else:
            self.editor.set_content(result.text, result.folds, starts=starts)
        self._update_status()
        self._update_fold_actions()
        self.loaded.emit()

    def loading_progress(self, fraction: float) -> None:
        if fraction < 1.0:
            self.status_label.setText(t("gui.text.loading_progress", percent=int(fraction * 100)))
        else:
            self._update_status()

    def _content_loaded(self) -> None:
        self._update_status()
        self._update_fold_actions()

    @property
    def _task(self) -> Any:
        """The formatting task this view waits for (``None``: none) -- kept for scripts."""
        return self._build[1].task if self._build is not None else None

    def is_loading(self) -> bool:
        """A node is being formatted, inserted or (un)folded (tests)."""
        return self._build is not None or self.editor.busy()

    def result(self) -> PrettyText | None:
        return self._result

    def text(self) -> str:
        """The display text of the shown node (complete, also while it is still being
        inserted or partly folded); ``""`` without one."""
        r = self._result
        if r is not None and not r.virtual:
            return r.text
        return self.editor.toPlainText()

    def _show_empty(self) -> None:
        self.editor.clear_content()
        self.editor.set_empty_text(t("gui.text.empty"))
        self.path_label.setText("")
        self.size_label.setText("")
        self.status_label.setText("")
        self._update_fold_actions()

    def _show_notices(self, r: PrettyText) -> None:
        parts: list[str] = []
        buttons: list[tuple[str, Any]] = []
        kind = "info"
        if r.error is not None:
            kind = "error"
            message = getattr(r.error, "message", str(r.error))
            parts.append(t("gui.text.json_error", message=message))
            if r.error_offset is not None:
                offset = r.error_offset
                buttons.append((t("gui.text.goto_error"), lambda o=offset: self.open_raw_requested.emit(o, 1)))
        if r.truncated:
            if kind != "error":
                kind = "warning"
            parts.append(t("gui.text.too_large", size=format_size(r.size), shown=format_size(r.shown_bytes)))
            buttons.append((t("gui.text.open_raw"), lambda: self.request_raw()))
        if r.cut_lines:
            parts.append(t("gui.text.long_lines", count=format_count(r.cut_lines), chars=format_count(MAX_LINE_CHARS)))
        if not parts:
            return
        self._show_bar(kind, parts[0], buttons, detail="\n".join(parts[1:]) or None)

    def _show_bar(self, kind: str, message: str, buttons: list[tuple[str, Any]] | None = None,
                  detail: str | None = None) -> None:
        bar = self.info_bar
        bar.set_kind(kind)
        bar.set_message(message)
        bar.set_detail(detail)
        bar.clear_buttons()
        for label, callback in buttons or []:
            bar.add_button(label, callback)
        bar.setVisible(True)

    def message(self) -> str | None:
        """The text of the visible info bar (tests)."""
        return self.info_bar.message() if self.info_bar.isVisibleTo(self) else None

    def request_raw(self) -> None:
        """Ask the document tab to show the node's bytes in a raw view."""
        r = self._result
        if r is None:
            return
        self.open_raw_requested.emit(int(r.start), int(r.size))

    # ------------------------------------------------------------------ status / actions
    def _update_status(self) -> None:
        if self.editor.loading:
            return
        if self._result is None or self._result.virtual:
            self.status_label.setText("")
            return
        cursor = self.editor.textCursor()
        block = cursor.block()
        col = _cp(block.text(), cursor.positionInBlock()) + 1
        self.status_label.setText(t("gui.text.status", line=format_count(self.editor.current_line() + 1),
                                    column=format_count(col),
                                    lines=format_count(self.editor.line_count())))

    def _fill_fold_menu(self, menu: QMenu) -> None:
        a = QAction(t("gui.text.fold_all"), menu)
        a.setObjectName("fold_all")
        a.triggered.connect(lambda _c=False: self.editor.fold_all())
        menu.addAction(a)
        a = QAction(t("gui.text.unfold_all"), menu)
        a.setObjectName("unfold_all")
        a.triggered.connect(lambda _c=False: self.editor.unfold_all())
        menu.addAction(a)
        menu.addSeparator()
        for level in range(1, FOLD_LEVELS + 1):
            a = QAction(t("gui.text.fold_level", level=level), menu)
            a.setObjectName(f"fold_level_{level}")
            a.triggered.connect(lambda _c=False, n=level: self.editor.fold_to_level(n))
            menu.addAction(a)

    def _update_fold_actions(self) -> None:
        has = len(self.editor.folds) > 0 and self._result is not None and not self._result.virtual
        self.fold_button.setEnabled(has)

    def build_context_menu(self) -> QMenu:
        menu = QMenu(self)
        cursor = self.editor.textCursor()
        copy = QAction(t("gui.text.menu.copy"), menu)
        copy.setObjectName("text_copy_selection")
        copy.setShortcut(QKeySequence(QKeySequence.StandardKey.Copy))   # shown only: the window owns Ctrl+C
        copy.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        copy.setShortcutVisibleInContextMenu(True)
        copy.setEnabled(cursor.hasSelection())
        copy.triggered.connect(lambda _c=False: self.copy_text())
        menu.addAction(copy)
        copy_all = QAction(t("gui.text.menu.copy_all"), menu)
        copy_all.setObjectName("text_copy_all")
        copy_all.setEnabled(self._result is not None and not self._result.virtual)
        copy_all.triggered.connect(lambda _c=False: self.copy_text(all_text=True))
        menu.addAction(copy_all)
        select_all = QAction(t("gui.text.menu.select_all"), menu)
        select_all.setObjectName("text_select_all")
        select_all.triggered.connect(lambda _c=False: self.editor.selectAll())
        menu.addAction(select_all)
        menu.addSeparator()
        fold = menu.addMenu(t("gui.text.fold"))
        self._fill_fold_menu(fold)
        fold.setEnabled(self.fold_button.isEnabled())
        wrap = QAction(t("gui.text.wrap"), menu)
        wrap.setCheckable(True)
        wrap.setChecked(self.wrap())
        wrap.toggled.connect(self.set_wrap)
        menu.addAction(wrap)
        if self._ref is not None:
            menu.addSeparator()
            try:
                from jsonreshape.gui.actions import node_menu

                sub = node_menu(menu, self.session, self._ref, view=self)
                sub.setTitle(t("gui.text.menu.node"))
                menu.addMenu(sub)
            except Exception:
                log.debug("node menu unavailable", exc_info=True)
        return menu

    # ------------------------------------------------------------------ copy
    def _selection_span(self) -> tuple[int, int, int, int] | None:
        """``(line, column, line, column)`` of the selection in source lines / code points."""
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            return None
        doc = self.editor.document()
        s, e = cursor.selectionStart(), cursor.selectionEnd()
        bs, be = doc.findBlock(s), doc.findBlock(e)
        ed = self.editor
        return (ed.line_of_block(bs.blockNumber()), _cp(bs.text(), s - bs.position()),
                ed.line_of_block(be.blockNumber()), _cp(be.text(), e - be.position()))

    def selected_full_text(self) -> str | None:
        """The complete text of the selection (folded lines inside it included, shortened
        tails taken whole; everything selected = the whole text); ``None`` without a
        selection or a text."""
        r = self._result
        span = self._selection_span()
        if r is None or r.virtual or span is None:
            return None
        cursor = self.editor.textCursor()
        if cursor.selectionStart() == 0 and cursor.selectionEnd() >= self.editor.document().characterCount() - 1:
            return r.copy_text
        return r.copy_range(*span)

    def copy_text(self, *, all_text: bool = False) -> str | None:
        """Copy the selection, or (no selection / ``all_text``) the whole pretty text --
        always the complete text, even where the screen shortens long lines. Returns the
        text."""
        r = self._result
        if not all_text:
            text = self.selected_full_text()
            if text is not None:
                return set_clipboard_text(text)
        if r is None or r.virtual:
            return None
        return set_clipboard_text(r.copy_text)

    def selected_text(self) -> str:
        """The selected text ("" when nothing is selected); prefills searches."""
        text = self.selected_full_text()
        return text if text is not None else ""

    def copy_value(self, fmt: str = "json") -> Any:
        """Ctrl+C: the selected text when there is a selection, else the node (``fmt``)."""
        if fmt == "json" and self.editor.textCursor().hasSelection():
            return self.copy_text()
        return super().copy_value(fmt)

    # ------------------------------------------------------------------ quick find
    def quick_find(self) -> None:
        cursor = self.editor.textCursor()
        text = cursor.selectedText() if cursor.hasSelection() else None
        if text and ("\u2029" in text or len(text) > 200):
            text = None
        self.find_bar.open_bar(text)

    def _clear_find(self) -> None:
        self._find_gen += 1
        if self._find_task is not None:
            self._find_task.cancel()
            self._find_task = None
        self._matches = Matches()
        self._match_cur = None
        self.editor.schedule_extras()

    def _on_find_changed(self, text: str, case: bool) -> None:
        self._clear_find()
        if not text:
            return
        matcher = self.find_bar.matcher()
        r = self._result
        if matcher is None or r is None or r.virtual:
            self.find_bar.set_status(status_text(None, 0), "notfound")
            return
        haystack = r.text
        gen = self._find_gen
        if len(haystack) <= SYNC_FIND_CHARS:
            self._found(gen, find_matches(haystack, matcher))
            return

        def work(ctx: Any) -> Matches:
            return find_matches(haystack, matcher, cancel=ctx.cancel)

        self.find_bar.set_status(t("gui.common.loading"))
        self._find_task = self.session.app.tasks.run(work, title=t("gui.text.task.find"), kind="find", owner=self,
                                                     silent=True, on_done=lambda m: self._found(gen, m))

    def _found(self, gen: int, matches: Matches) -> None:
        if gen != self._find_gen:
            return
        self._find_task = None
        self._matches = matches
        if not len(matches):
            self._match_cur = None
            self.find_bar.set_status(status_text(None, 0), "notfound")
            self.editor.schedule_extras()
            return
        cursor = self.editor.textCursor()
        here = (self.editor.current_line(),
                _cp(cursor.block().text(), cursor.selectionStart() - cursor.block().position()))
        i = self._match_index_at_or_after(here)
        self._goto_match(i if i is not None else 0)

    def _match_index_at_or_after(self, pos: tuple[int, int]) -> int | None:
        m = self._matches
        line, col = pos
        i = bisect.bisect_left(m.lines, line)
        while i < len(m) and m.lines[i] == line and m.cols[i] < col:
            i += 1
        return i if i < len(m) else None

    def match_count(self) -> int:
        return len(self._matches)

    def current_match(self) -> int | None:
        return self._match_cur

    def find_next(self, backwards: bool = False) -> None:
        if not len(self._matches):
            if not self.find_bar.isVisible():
                self.quick_find()
            return
        n = len(self._matches)
        if self._match_cur is None:
            i = 0 if not backwards else n - 1
        else:
            i = (self._match_cur + (-1 if backwards else 1)) % n
        self._goto_match(i)

    def _goto_match(self, i: int) -> None:
        """Select match ``i``: folds hiding it open, and it is selected as soon as its
        line is inserted (the GUI never inserts the rest of the text at once)."""
        m = self._matches
        line, col, length = m.lines[i], m.cols[i], m.lens[i]
        ed = self.editor
        self._match_cur = i
        ed.reveal_line(line)
        ed.set_goal(select=(line, col, length))
        ed.schedule_extras()
        self.find_bar.set_status(status_text(i, len(m), capped=m.capped), "found")

    def match_selections(self, first: int, last: int) -> list[QTextEdit.ExtraSelection]:
        """Extra selections of the matches on source lines ``first..last`` (the visible part)."""
        m = self._matches
        if not len(m):
            return []
        out = []
        lo = bisect.bisect_left(m.lines, first)
        hi = bisect.bisect_right(m.lines, last)
        if hi - lo > 2000:
            hi = lo + 2000
        ed = self.editor
        normal = QTextCharFormat()
        normal.setBackground(ed._color("match"))
        current = QTextCharFormat()
        current.setBackground(ed._color("current_match"))
        block = QTextBlock()
        block_line = -1
        for k in range(lo, hi):
            line = m.lines[k]
            if line != block_line:
                block = ed.block_for_line(line)
                block_line = line
            if not block.isValid():
                continue
            text = block.text()
            sel = QTextEdit.ExtraSelection()
            c = QTextCursor(block)
            c.setPosition(block.position() + _u16(text, min(m.cols[k], len(text))))
            c.setPosition(block.position() + _u16(text, min(m.cols[k] + m.lens[k], len(text))),
                          QTextCursor.MoveMode.KeepAnchor)
            sel.cursor = c
            sel.format = current if k == self._match_cur else normal
            out.append(sel)
        return out

    # ------------------------------------------------------------------ lifetime
    def shutdown(self) -> None:
        if self._dead:
            return
        self._dead = True
        self._gen += 1
        self._find_gen += 1
        self._leave_build()
        self._find_task = None
        self._caret_timer.stop()
        self.editor.stop()
        settings_changed = getattr(self.session.app, "settings_changed", None)
        if settings_changed is not None:
            try:
                settings_changed.disconnect(self._on_settings_changed)
            except (RuntimeError, TypeError):
                pass
        super().shutdown()


def _identity(ref: Any) -> tuple | None:
    """``ref``'s child indices when complete: two members with the same (duplicate) key
    are different texts; ``None``: the node the path resolves to."""
    if not ref.path:
        return ()
    ipath = getattr(ref, "ipath", None)
    return tuple(ipath) if ipath is not None and len(ipath) == len(ref.path) else None


def _same(a: Any, b: Any) -> bool:
    return a.record == b.record and tuple(a.path) == tuple(b.path) and _identity(a) == _identity(b)
