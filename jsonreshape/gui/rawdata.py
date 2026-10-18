"""Qt-free data logic of the raw view (docs/ARCHITECTURE.md §11.7 "Raw").

* :class:`LineIndex` -- newline checkpoints of a data file (``(offset, line)`` every
  :data:`CHECKPOINT_BYTES`), built by counting ``b"\\n"`` in 8 MiB blocks, usable for the
  part already built; :func:`line_index_for` caches one per :class:`~jsonreshape.reader.Document`.
* the visual **row rule** (:func:`row_start_at`, :func:`next_row_start`, :func:`walk_rows`,
  :func:`rows_back`) -- how text lines are cut into bounded rows;
* :func:`decode_row` / :func:`display_text` -- UTF-8 decoding of one row with a
  char -> byte map, never producing a replacement character for a valid character.

Row rule (exact)
----------------
A *line* ends after a ``\\n`` byte (or at the end of the file). ``W`` = the row width in
bytes (:class:`RowRule`; the raw view fits it to the window width, :data:`ROW_BYTES` by
default), ``A`` = ``64 * W``. For a line starting at ``S`` the
*nominal* row boundaries are

* ``S + k*W`` for ``0 < k*W <= A`` (rows measured from the line start), then
* every multiple of ``W`` (absolute file offset) greater than ``S + A``.

Each nominal boundary ``b`` is *adjusted* to a character start: ``b - j`` for the smallest
``j`` in ``0..3`` such that the byte at ``b - j`` is not a UTF-8 continuation byte
(``0x80..0xBF``) and ``b - j >= S``; when there is none, ``b`` itself. The row starts of the
line are ``S`` plus the adjusted boundaries (equal values merge). Rows therefore start
after a newline or at a ``W``-byte step, never split a valid multi-byte character, and are
at most ``W + 3`` bytes long.

The row containing offset ``x`` only depends on the bytes in
``[x - L, x + 4)`` (``L`` = ``A + W + 8``, :attr:`RowRule.lookback`): when there is no newline in
that range the line started more than ``A + W`` bytes earlier and only the absolute
boundaries matter. So rows are identical whether a page is reached scrolling down, up or
by a jump -- the global row number is never needed (a 5 GB single-line file has ~20 M rows).
Another ``W`` (the window was resized) gives other rows; the view then keeps the row
containing its old first byte on top.
"""

from __future__ import annotations

import bisect
import logging
import re
import threading
import weakref
from array import array
from typing import Any, BinaryIO, Callable, NamedTuple

from jsonreshape.errors import FileChangedError
from jsonreshape.tasks import CancelToken, Progress

log = logging.getLogger(__name__)

__all__ = [
    "ANCHOR_BYTES",
    "ANCHOR_ROWS",
    "DEFAULT_RULE",
    "MAX_ROW_BYTES",
    "MIN_ROW_BYTES",
    "RowRule",
    "need_back",
    "need_forward",
    "BLOCK_BYTES",
    "CHECKPOINT_BYTES",
    "LOOKBACK",
    "ROW_BYTES",
    "LineIndex",
    "decode_row",
    "display_text",
    "is_line_start",
    "line_index_for",
    "next_row_start",
    "row_start_at",
    "rows_back",
    "walk_rows",
]

ROW_BYTES = 256                 # W: default nominal bytes per visual row
MIN_ROW_BYTES = 32
MAX_ROW_BYTES = 1024
ANCHOR_ROWS = 64                # A = 64 * W: rows are measured from the line start for this many bytes
ANCHOR_BYTES = ANCHOR_ROWS * ROW_BYTES
LOOKBACK = ANCHOR_BYTES + ROW_BYTES + 8
CHECKPOINT_BYTES = 1 << 20      # LineIndex: one (offset, line) checkpoint per MiB
BLOCK_BYTES = 8 << 20           # LineIndex: read size while building

_NL = b"\n"


# ==================================================================================
# row rule
# ==================================================================================
class RowRule(NamedTuple):
    """Parameters of the row rule: ``w`` nominal bytes per row, ``anchor`` bytes measured
    from the line start (a multiple of ``w``)."""

    w: int
    anchor: int

    @property
    def lookback(self) -> int:
        """Bytes before an offset that decide its row."""
        return self.anchor + self.w + 8

    @classmethod
    def for_width(cls, w: int) -> RowRule:
        """The rule for rows of ``w`` bytes (clamped to MIN_ROW_BYTES..MAX_ROW_BYTES)."""
        w = max(MIN_ROW_BYTES, min(MAX_ROW_BYTES, int(w)))
        return cls(w, ANCHOR_ROWS * w)


DEFAULT_RULE = RowRule(ROW_BYTES, ANCHOR_BYTES)


def _line_start(buf: bytes, base: int, x: int, rule: RowRule) -> int | None:
    """Start of the line containing ``x``: after the last newline in ``[x - lookback, x)``;
    ``0`` when that range reaches the file start; ``None`` (far) otherwise."""
    lo = max(0, x - rule.lookback)
    p = buf.rfind(_NL, lo - base, x - base)
    if p >= 0:
        return base + p + 1
    return 0 if lo == 0 else None


def _nominal_after(r: int, s: int | None, rule: RowRule) -> int:
    """The smallest nominal boundary greater than ``r`` of the line starting at ``s``."""
    w, a = rule
    if s is not None and r < s + a:
        return s + ((r - s) // w + 1) * w
    floor = r if s is None else max(r, s + a)
    return (floor // w + 1) * w


def _nominal_at_or_before(x: int, s: int | None, rule: RowRule) -> int:
    """The largest nominal boundary (or the line start) not greater than ``x``."""
    w, a = rule
    if s is not None and x <= s + a:
        return s + ((x - s) // w) * w
    m = (x // w) * w
    if s is not None and m <= s + a:
        return s + a
    return m


def _adjust(buf: bytes, base: int, b: int, s: int | None, size: int) -> int:
    """Nominal boundary ``b`` moved back to a character start (see the module doc)."""
    if b >= size:
        return size
    for j in range(4):
        q = b - j
        if (s is not None and q < s) or q < base:
            break
        if not 0x80 <= buf[q - base] < 0xC0:
            return q
    return b


def is_line_start(buf: bytes, base: int, x: int) -> bool:
    """``x`` is the first byte of a line (needs the byte before ``x`` in ``buf``)."""
    return x == 0 or buf[x - 1 - base] == 0x0A


def row_start_at(buf: bytes, base: int, x: int, size: int, rule: RowRule = DEFAULT_RULE) -> int:
    """Start of the visual row containing byte ``x`` (``0 <= x < size``). ``buf`` holds the
    file bytes from ``base`` and must cover ``[max(0, x - rule.lookback), min(size, x + 4))``."""
    if x >= size:
        x = size - 1
    if x <= 0:
        return 0
    s = _line_start(buf, base, x, rule)
    best = s if s is not None else -1
    nb = _nominal_at_or_before(x, s, rule)
    while nb - 3 <= x and nb < size:
        if s is None or nb > s:
            a = _adjust(buf, base, nb, s, size)
            if best < a <= x:
                best = a
        nb = _nominal_after(nb, s, rule)
    return max(best, 0)


def next_row_start(buf: bytes, base: int, r: int, size: int, s: int | None,
                   rule: RowRule = DEFAULT_RULE) -> int:
    """Start of the row after the row starting at ``r`` (``size`` at the end). ``s``: the
    start of ``r``'s line (``None``: far before). ``buf`` must cover
    ``[r - 3, min(size, r + rule.w + 8))``."""
    if r >= size:
        return size
    hi = min(size, r + rule.w + 8)
    p = buf.find(_NL, r - base, hi - base)
    nl = base + p + 1 if p >= 0 else size + 1
    nb = _nominal_after(r, s, rule)
    while True:
        if nb >= size:
            cand = size
            break
        a = _adjust(buf, base, nb, s, size)
        if a > r:
            cand = a
            break
        nb = _nominal_after(nb, s, rule)
    return min(nl, cand)


def walk_rows(buf: bytes, base: int, start: int, size: int, count: int,
              rule: RowRule = DEFAULT_RULE) -> list[int]:
    """Row boundaries from the row start ``start``: ``[start, b1, b2, ...]`` with up to
    ``count`` rows (row ``i`` = ``[out[i], out[i+1])``); shorter at the end of the file.
    ``buf`` must cover ``[max(0, start - rule.lookback), min(size, start + need_forward(count, rule)))``."""
    out = [start]
    if start >= size:
        return out
    s = _line_start(buf, base, start, rule) if start > 0 else 0
    r = start
    while len(out) <= count and r < size:
        n = next_row_start(buf, base, r, size, s, rule)
        if buf[n - 1 - base] == 0x0A:
            s = n
        out.append(n)
        r = n
    return out


def rows_back(buf: bytes, base: int, x: int, size: int, count: int, rule: RowRule = DEFAULT_RULE) -> int:
    """The row start ``count`` rows above the row starting at ``x`` (0 at the file start).
    ``buf`` must cover ``[max(0, x - need_back(count, rule)), min(size, x + 4))``."""
    for _ in range(count):
        if x <= 0:
            return 0
        x = row_start_at(buf, base, x - 1, size, rule)
    return x


def need_back(count: int, rule: RowRule = DEFAULT_RULE) -> int:
    """Bytes before a row start that :func:`rows_back` may read for ``count`` rows."""
    return count * (rule.w + 4) + rule.lookback + 8


def need_forward(count: int, rule: RowRule = DEFAULT_RULE) -> int:
    """Bytes after a row start that :func:`walk_rows` may read for ``count`` rows."""
    return count * (rule.w + 4) + rule.w + 16


# ==================================================================================
# decoding
# ==================================================================================
# one UTF-8 character (strict: no overlongs, no surrogates, <= U+10FFFF) or one invalid byte
_UTF8_CHAR = re.compile(
    rb"[\x00-\x7f]|[\xc2-\xdf][\x80-\xbf]|\xe0[\xa0-\xbf][\x80-\xbf]|[\xe1-\xec\xee\xef][\x80-\xbf]{2}"
    rb"|\xed[\x80-\x9f][\x80-\xbf]|\xf0[\x90-\xbf][\x80-\xbf]{2}|[\xf1-\xf3][\x80-\xbf]{3}"
    rb"|\xf4[\x80-\x8f][\x80-\xbf]{2}|[\x80-\xff]")

REPLACEMENT = "\ufffd"

#: Characters drawn with a visible symbol (same length: one char each).
_CONTROL_MAP = {c: "·" for c in list(range(0x00, 0x20)) + [0x7F] + list(range(0x80, 0xA0))}
_CONTROL_MAP[0x09] = "→"     # tab: →
_CONTROL_MAP.update({0x2028: "·", 0x2029: "·", 0xFEFF: "·", 0x200B: "·"})
_DISPLAY_TABLE = str.maketrans(_CONTROL_MAP)


def decode_row(data: bytes) -> tuple[str, list[int] | None]:
    """``(text, starts)``: ``data`` decoded as UTF-8, each invalid byte as one U+FFFD;
    ``starts[i]`` = byte offset of character ``i`` in ``data`` (``len(text) + 1`` items, the
    last is ``len(data)``); ``None`` when every character is one byte."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None:
        if len(text) == len(data):
            return text, None
        starts = [0] * (len(text) + 1)
        pos = 0
        for i, ch in enumerate(text):
            starts[i] = pos
            o = ord(ch)
            pos += 1 if o < 0x80 else 2 if o < 0x800 else 3 if o < 0x10000 else 4
        starts[-1] = pos
        return text, starts
    chars: list[str] = []
    starts = []
    pos = 0
    for tok in _UTF8_CHAR.findall(data):
        starts.append(pos)
        pos += len(tok)
        if len(tok) == 1 and tok[0] >= 0x80:
            chars.append(REPLACEMENT)
        else:
            chars.append(tok.decode("utf-8"))
    starts.append(pos)
    return "".join(chars), starts


def display_text(text: str) -> str:
    """``text`` with control / invisible characters replaced by a visible symbol (same
    length): tab ``→``, other C0/C1 controls, DEL, U+2028/2029, BOM ``·``."""
    return text.translate(_DISPLAY_TABLE)


def is_control(ch: str) -> bool:
    """``ch`` is drawn as a symbol by :func:`display_text`."""
    return ord(ch) in _CONTROL_MAP


# ==================================================================================
# LineIndex
# ==================================================================================
class LineIndex:
    """Newline checkpoints of one data file (thread-safe; Qt-free).

    ``checkpoint(k)`` = the 1-based number of the line containing byte ``k *
    CHECKPOINT_BYTES``. Built by :meth:`build` (resumable after a cancel: it continues
    at the last checkpoint); queries work for the part built so far. Lines are separated
    by ``b"\\n"``: a file with ``n`` newlines has ``n + 1`` lines (the last one may be empty,
    starting at the end of the file).
    """

    def __init__(self, size: int, *, checkpoint_bytes: int = CHECKPOINT_BYTES) -> None:
        if checkpoint_bytes <= 0:
            raise ValueError("checkpoint_bytes must be positive")
        self.size = int(size)
        self.step = int(checkpoint_bytes)
        self._lines = array("q", [1])     # line number at offset k * step
        self._built = 0                   # bytes [0, built) counted
        self._line_at_built = 1           # line number at offset `built`
        self._complete = self.size == 0
        self._building = False
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"<LineIndex size={self.size} built={self._built} complete={self._complete}>"

    # -- state ---------------------------------------------------------------------------
    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def built(self) -> int:
        """Bytes counted so far."""
        return self._built

    @property
    def building(self) -> bool:
        return self._building

    @property
    def line_count(self) -> int | None:
        """Number of lines (newlines + 1) once complete, else ``None``."""
        return self._line_at_built if self._complete else None

    def known_lines(self) -> int:
        """Lines whose start offset is known: every line starting in the counted part."""
        return self._line_at_built

    def covers_offset(self, offset: int) -> bool:
        return self._complete or 0 <= offset <= self._built

    def covers_line(self, line: int) -> bool:
        return 1 <= line <= self._line_at_built

    def fraction(self) -> float:
        return 1.0 if self._complete or self.size <= 0 else min(1.0, self._built / self.size)

    def checkpoints(self) -> list[tuple[int, int]]:
        """``[(offset, line), ...]`` (a copy)."""
        with self._lock:
            return [(k * self.step, int(line)) for k, line in enumerate(self._lines)]

    # -- building ------------------------------------------------------------------------
    def try_begin(self) -> bool:
        """Reserve the build (False when another build runs or the index is complete)."""
        with self._lock:
            if self._building or self._complete:
                return False
            self._building = True
            return True

    def end(self) -> None:
        with self._lock:
            self._building = False

    def build(self, f: BinaryIO, *, progress: Progress | None = None, cancel: CancelToken | None = None,
              block_bytes: int = BLOCK_BYTES, on_block: Callable[[int, int], Any] | None = None,
              reserved: bool = False) -> None:
        """Count the newlines of ``f`` (an open binary handle on the data file) from the
        last checkpoint to :attr:`size`; ``progress`` in bytes, ``cancel`` checked per
        block; ``on_block(built, known_lines)`` after each block. ``reserved``: the caller
        already holds :meth:`try_begin`."""
        if not reserved and not self.try_begin():
            if self._complete:
                return
            raise RuntimeError("the line index is already being built")
        try:
            self._build(f, progress, cancel, max(self.step, int(block_bytes)), on_block)
        finally:
            self.end()

    def _build(self, f: BinaryIO, progress: Progress | None, cancel: CancelToken | None, block_bytes: int,
               on_block: Callable[[int, int], Any] | None) -> None:
        with self._lock:
            k = len(self._lines) - 1
            pos = k * self.step
            line = int(self._lines[k])
        size = self.size
        if progress is not None:
            progress.set_total(size)
            progress.update(pos)
        f.seek(pos)
        while pos < size:
            if cancel is not None:
                cancel.check()
            want = min(block_bytes, size - pos)
            block = f.read(want)
            if not block or len(block) < want:
                raise FileChangedError(path=str(getattr(f, "name", "")))
            n = len(block)
            new: list[int] = []
            i = 0
            while (k + 1) * self.step <= pos + n:
                cut = (k + 1) * self.step - pos
                line += block.count(_NL, i, cut)
                i = cut
                k += 1
                new.append(line)
            line += block.count(_NL, i)
            pos += n
            with self._lock:
                self._lines.extend(new)
                self._built = pos
                self._line_at_built = line
            if progress is not None:
                progress.update(pos)
            if on_block is not None:
                on_block(pos, line)
        with self._lock:
            self._complete = True
        if progress is not None:
            progress.finish()

    # -- queries -------------------------------------------------------------------------
    def line_of(self, offset: int, f: BinaryIO) -> int:
        """1-based line number of the line containing byte ``offset`` (``offset ==
        size``: the last line). Reads at most one checkpoint step from ``f``.
        ``ValueError`` when ``offset`` is outside the counted part."""
        if offset < 0 or offset > self.size or not self.covers_offset(offset):
            raise ValueError(f"offset {offset} is not covered by the line index")
        with self._lock:
            k = min(offset // self.step, len(self._lines) - 1)
            line = int(self._lines[k])
        start = k * self.step
        if offset > start:
            f.seek(start)
            data = f.read(offset - start)
            if len(data) < offset - start:
                raise FileChangedError(path=str(getattr(f, "name", "")))
            line += data.count(_NL)
        return line

    def line_start(self, line: int, f: BinaryIO) -> int:
        """Offset of the first byte of line ``line`` (1-based; the last line may start at
        ``size``). Reads at most about one checkpoint step from ``f``. ``ValueError`` when
        the line is not covered (yet) or does not exist."""
        if line < 1 or not self.covers_line(line):
            raise ValueError(f"line {line} is not covered by the line index")
        if line == 1:
            return 0
        with self._lock:
            # the last checkpoint whose line is before `line`: line `line` starts after it
            k = bisect.bisect_left(self._lines, line) - 1
            base_line = int(self._lines[k])
            limit = self._built
        pos = k * self.step
        need = line - base_line            # newlines to pass after pos
        f.seek(pos)
        while need > 0 and pos < limit:
            chunk = f.read(min(self.step, limit - pos))
            if not chunk:
                raise FileChangedError(path=str(getattr(f, "name", "")))
            c = chunk.count(_NL)
            if c >= need:
                i = -1
                for _ in range(need):
                    i = chunk.index(_NL, i + 1)
                return pos + i + 1
            need -= c
            pos += len(chunk)
        raise ValueError(f"line {line} is not covered by the line index")


_INDEXES: "weakref.WeakKeyDictionary[Any, LineIndex]" = weakref.WeakKeyDictionary()
_INDEXES_LOCK = threading.Lock()


def line_index_for(doc: Any) -> LineIndex:
    """The :class:`LineIndex` of ``doc``'s data file (one per document, created empty on
    first use, dropped with the document)."""
    with _INDEXES_LOCK:
        idx = _INDEXES.get(doc)
        if idx is None:
            idx = LineIndex(int(doc.data_size))
            _INDEXES[doc] = idx
        return idx
