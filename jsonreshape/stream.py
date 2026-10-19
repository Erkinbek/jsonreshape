"""Streaming access to huge records with ijson (docs/ARCHITECTURE.md §5.3).

Records larger than ``Settings.large_node_bytes`` are never loaded whole
(:meth:`jsonreshape.reader.Document.record_bytes` refuses them and
:meth:`~jsonreshape.reader.Document.iter_blocks` yields them as ``huge`` blocks). The
functions here parse such a record incrementally with ``ijson.basic_parse``
(``yajl2_c`` backend, ``use_float=False``) over a range reader on ``doc.open_data()`` (the
document's virtual address space: the data file, or the edit layer for an edited record --
docs/ARCHITECTURE.md §12.3):

* :func:`iter_events` -- the raw ijson events of a span;
* :func:`stream_values` -- the values of several :class:`~jsonreshape.paths.PathPattern`
  (what ``Filter.matches_values`` needs) without building the whole record;
* :func:`stream_probe` -- feeds the values matched by patterns to *probes* (objects that
  decide something on the fly, e.g. "some value equals 7" or "the length of the first
  value"), without building the matched containers or keeping the values;
* :func:`stream_text_prefix` -- the beginning of one matched value (a bounded prefix of
  a huge string, or of a container's compact JSON text);
* :func:`stream_walk` -- every key and scalar in document order (full-text search).

``member=True`` treats the span as an object member ``"key": value`` (keyed records):
it is parsed as ``{`` + span + ``}`` and paths are relative to the member *value*.

Numbers are converted so that types match :func:`jsonreshape.codec.loads`: integers in
the 64-bit range stay ``int``, larger ones become ``JsonInt``; ``Decimal`` values go
through ``codec.loads(str(d))`` (``JsonFloat`` or ``float``; equal values -- the raw
text of exponents may be normalized, e.g. ``1e400`` -> ``1E+400``). ijson limitations
(documented deviations from ``codec.loads``): ``-0`` arrives as ``0``; a lone surrogate
escape -- high (``\\ud800``) or low (``\\udc00``) -- arrives as ``"?"``; a string value
always arrives whole (one ijson event), so a huge *string* is held in memory once while it
is examined. A number whose exponent is beyond what ``Decimal`` can hold (about ``1e10**18``,
e.g. ``1e99999999999999999999``) makes the record unreadable (``stream.error.parse``).

Tokens yajl2_c mishandles
-------------------------
ijson's ``yajl2_c`` backend builds a Python ``int`` for every integer token. For a token of
more digits than Python converts (``sys.get_int_max_str_digits()``, 4300 by default) that
conversion fails, but the backend still emits a ``("number", NULL)`` event with the error left
pending: reading that event's value dereferences NULL and kills the process (a Windows access
violation; review R5A-X1); other consumers get a ``SystemError``. A lone low surrogate escape
makes it raise ``UnicodeDecodeError`` (cleanly). So the bytes are never fed as they are when
they could hold such a token: every chunk is checked for a digit run longer than
:data:`SAFE_INT_DIGITS` (640: no ``int(str)`` limit can be lower) -- ~2 % of the parse time.
When one is ahead (or the backend raised ``UnicodeDecodeError``), the record is parsed again
from its start in *careful mode* and the events already delivered are skipped: a lexical
filter (:class:`_CarefulFilter`; it tracks strings and escapes across chunks) gives an integer
token longer than :data:`SAFE_INT_DIGITS` digits the exponent ``e0`` (the backend then builds an
exact ``Decimal``, and ``codec.loads(str(d))`` is what ``codec.loads`` gives the original token)
and turns a lone low surrogate escape into ``\\u003f`` (``"?"``, like yajl2_c's lone high ones);
digit runs inside strings are left alone. The token structure never changes, so the events of
both passes correspond one to one. :func:`exact_events` is an exact pure-Python parser of a
whole JSON text (``codec.loads`` semantics, no recursion) and :func:`needs_exact` tells whether
a text holds anything yajl2_c mishandles (the reader's head analysis uses both).

Bounded memory
--------------
:func:`stream_values` reproduces ``PathPattern.values(codec.loads(record))`` exactly,
including dict semantics for duplicate keys (the last occurrence wins, at the position
of the first). It keeps nothing but the captured values: in document order, one list
per pattern, without path or position tuples. Only when a duplicate key appears on a
path a pattern can still match (rare) is the record streamed a second time with the
exact algorithm that remembers every captured value's position. Matched containers are
built in memory (ijson ``ObjectBuilder``); ``first_only=True`` keeps just the first value
of every pattern (what a sort key needs). ``max_bytes`` bounds the estimated size of
everything captured (Python objects, roughly: a string costs its length plus ~60 bytes,
other scalars ~40 bytes, a container ~60 bytes plus its members; the duplicate-key
bookkeeping is counted as well): when the captured values would exceed it,
:class:`CaptureLimitExceeded` is raised (a localized error; callers skip the record with
a counted warning instead of using unbounded memory). :func:`capture_budget` gives the
budget for a huge record: ``min(Settings.large_node_bytes, Settings.memory_limit_bytes //
4)``, at least :data:`CAPTURE_MIN_BYTES` (1 MiB).

Read size
---------
The span is fed to ijson's push parser (``ijson.basic_parse_coro``) in chunks of
:data:`BUF_SIZE` (64 KiB): the C backend turns a whole chunk into event objects at once
(~20 MB of tuples per MiB of small values), so a small chunk bounds that memory. A chunk
that produces no event lies inside one token (or whitespace), and yajl re-lexes an
unfinished token from its start on every chunk -- a long string read 64 KiB at a time
would cost time quadratic in its length. So while a string (or number) stays open, the
chunk size doubles, up to :data:`BUF_MAX` (16 MiB), and such a chunk is cut right after
the end of the open token (the first unescaped ``"`` of a string, the first byte after a
number): the chunk that completes the token adds at most one more event, and the size
drops back to 64 KiB. A run of whitespace costs nothing to re-read and keeps 64 KiB
chunks. A 100 MB string thus streams in well under a second (instead of ~25 s) and needs
at most :data:`BUF_MAX` bytes of read buffer besides the string itself.

:func:`stream_probe` keeps no values at all for the probed patterns (probes see each
matched scalar, and the start, direct children and end of each matched container). A
duplicate key on a path a probed pattern can match raises :class:`DuplicateKeys` --
probes cannot take back what they have seen; the caller evaluates the record from
:func:`stream_values` instead.

:func:`stream_walk` equals a pre-order walk of the parsed record for records without
duplicate keys; with duplicate keys it reports every occurrence in document order (dict
semantics would need the whole object in memory).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from decimal import Decimal
from typing import Any, BinaryIO, Iterable, Iterator

import ijson

from . import codec
from .errors import FileChangedError, JsonReshapeError
from .fileio import open_shared
from .paths import PathPattern, Wild, canonical_index, parse_pattern
from .tasks import CancelToken, check, format_bytes

log = logging.getLogger(__name__)

__all__ = [
    "CAPTURE_MIN_BYTES",
    "SAFE_INT_DIGITS",
    "CaptureLimitExceeded",
    "DuplicateKeys",
    "StreamProbe",
    "capture_budget",
    "exact_events",
    "iter_events",
    "needs_exact",
    "stream_probe",
    "stream_text_prefix",
    "stream_values",
    "stream_walk",
]

#: ijson chunk size: ijson's C backend turns a whole chunk into event objects at once
#: (~20 MB of tuples per MiB of small values), so a small chunk bounds that memory (and is
#: faster: 64 KiB measured ~54 MB/s against ~38 MB/s with 1 MiB)
BUF_SIZE = 1 << 16
#: largest chunk while a token stays open (module docstring, "Read size")
BUF_MAX = 1 << 24
CHECK_EVENTS = 1 << 14     # events between cancellation checks
_INT_MIN = -(2**63)
_UINT_MAX = 2**64 - 1
_STEP_CACHE_MAX = 1 << 16  # cached NFA steps per pattern

# estimated in-memory bytes of captured data (see "Bounded memory")
_COST_STR = 57             # str object header + list slot
_COST_SCALAR = 40          # number object + list slot (true/false/null: the slot only)
_COST_KEY = 120            # member key: str header + dict entry
_COST_CONTAINER = 64       # empty dict / list object
_COST_LITERAL = 8


CAPTURE_MIN_BYTES = 1 << 20  # floor of capture_budget(): tiny settings would otherwise skip every huge record

#: an integer token of at most this many digits is converted by every ``int(str)`` limit
#: (``sys.set_int_max_str_digits`` refuses values below it); longer digit runs are never fed to
#: yajl2_c as they are (module docstring, "Tokens yajl2_c mishandles")
SAFE_INT_DIGITS = int(sys.int_info.str_digits_check_threshold)
_DIGITS = b"0123456789"
_DIGIT_MAP = bytes(0 if 0x30 <= b <= 0x39 else 1 for b in range(256))   # digits -> 0, other bytes -> 1
_LONG_RUN = bytes(SAFE_INT_DIGITS + 1)                                     # that many digits, translated
_HIGH_ESCAPE = re.compile(rb"\\u[dD][89abAB][0-9a-fA-F]{2}")
_LOW_ESCAPE = re.compile(rb"\\u[dD][c-fC-F][0-9a-fA-F]{2}")
_PARTIAL_ESCAPE = re.compile(rb"(?:u[0-9a-fA-F]{0,3})?")    # what may follow an escape's backslash at a chunk end
_QUESTION_ESCAPE = b"\\u003f"
# needs_exact(): surrogate escapes, plain pairs (a high one followed by a low one) and the doubtful
# contexts of a pair (a backslash before it: maybe an escaped backslash; a high one before it)
_SURROGATE_ESCAPE = re.compile(rb"\\u[dD][89a-fA-F][0-9a-fA-F]{2}")
_SURROGATE_PAIR = re.compile(rb"\\u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}")
_SURROGATE_DOUBT = re.compile(rb"\\\\u[dD][89a-fA-F]|\\u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][89abAB]")


def capture_budget(settings: Any) -> int:
    """The ``max_bytes`` callers pass for a huge record (the overall memory budget's
    share): ``min(Settings.large_node_bytes, Settings.memory_limit_bytes // 4)``, at least
    :data:`CAPTURE_MIN_BYTES`."""
    return max(CAPTURE_MIN_BYTES, min(int(settings.large_node_bytes), int(settings.memory_limit_bytes) // 4))


class CaptureLimitExceeded(JsonReshapeError):
    """The values a streamed record would have to keep in memory exceed the byte budget
    (``params["limit"]``: the budget, formatted). Callers skip the record with a counted
    warning ("record #N is too large to evaluate this expression in memory")."""

    key = "stream.error.capture_limit"


class DuplicateKeys(Exception):
    """:func:`stream_probe` met a duplicate object key on a path a probed pattern can
    match; probes cannot honour dict semantics (the last occurrence wins), so the caller
    must evaluate the record from :func:`stream_values` instead."""


class StreamProbe:
    """Protocol of the objects :func:`stream_probe` feeds (subclassing is optional).

    For every node a pattern matches, in document order: ``value(v)`` for a scalar
    (numbers typed like :func:`jsonreshape.codec.loads`); ``open(is_map)`` when an object
    or array starts -- return ``True`` to receive its *direct* children: ``child("key",
    k)`` for each member key of an object, ``child("value", v)`` for each scalar element
    of an array and ``child("container", None)`` for each element that is an object or
    array -- and then ``close()`` when that container ends. Calls for nested matched
    containers nest properly (``open``/``close`` pairs)."""

    def value(self, v: Any) -> None:  # pragma: no cover - protocol
        pass

    def open(self, is_map: bool) -> bool:  # pragma: no cover - protocol
        return False

    def child(self, kind: str, v: Any) -> None:  # pragma: no cover - protocol
        pass

    def close(self) -> None:  # pragma: no cover - protocol
        pass


class _RangeReader:
    """File-like ``read(n)`` over ``prefix + file[start:end] + suffix``."""

    def __init__(self, f: BinaryIO, start: int, end: int, prefix: bytes = b"", suffix: bytes = b"") -> None:
        self._f = f
        self._args = (start, end, prefix, suffix)
        self._pos = start
        self._end = end
        self._prefix = prefix
        self._suffix = suffix
        f.seek(start)

    def again(self) -> _RangeReader:
        """A reader of the same bytes from their start (careful mode's second pass)."""
        return _RangeReader(self._f, *self._args)

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = 1 << 62
        out = b""
        if self._prefix:
            out, self._prefix = self._prefix[:n], self._prefix[n:]
            n -= len(out)
        if n > 0 and self._pos < self._end:
            want = min(n, self._end - self._pos)
            data = self._f.read(want)
            if len(data) != want:
                raise FileChangedError(path=str(getattr(self._f, "name", "")))
            self._pos += len(data)
            out += data
            n -= len(data)
        if n > 0 and self._pos >= self._end and self._suffix:
            tail, self._suffix = self._suffix[:n], self._suffix[n:]
            out += tail
        return out


# the rest of an open string up to its closing quote (possessive: no backtracking when the
# chunk holds no closing quote)
_STRING_REST = re.compile(rb'[^"\\]*+(?:\\.[^"\\]*+)*+"', re.DOTALL)
_NUMBER_END = re.compile(rb"[^0-9+\-.eE]")          # the first byte after an open number
# a chunk without events that lies outside a string: separators, then perhaps the start of
# a number (a number ends at the next separator, which would have produced an event)
_OUTSIDE_STRING = re.compile(rb"[ \t\r\n,:]*+[0-9+\-.eE]*+")
_SEPARATORS = frozenset(b" \t\r\n,:")


class _Careful(Exception):
    """Internal: a digit run too long to hand to yajl2_c as it is lies ahead (module
    docstring, "Tokens yajl2_c mishandles")."""


def _digit_run(data: bytes, carry: int) -> int:
    """Normal mode's check of one chunk: the length of the digit run at its end (``carry`` =
    the run at the end of the data sent before it); :class:`_Careful` when the chunk holds or
    continues a run of more than :data:`SAFE_INT_DIGITS` digits (in a string or not)."""
    n = len(data)
    if not n:
        return carry
    lead = n - len(data.lstrip(_DIGITS)) if 0x30 <= data[0] <= 0x39 else 0
    if lead == n:
        carry += n
        if carry > SAFE_INT_DIGITS:
            raise _Careful()
        return carry
    if carry + lead > SAFE_INT_DIGITS or data.translate(_DIGIT_MAP).find(_LONG_RUN) >= 0:
        raise _Careful()
    return n - len(data.rstrip(_DIGITS)) if 0x30 <= data[-1] <= 0x39 else 0


class _CarefulFilter:
    """Careful mode's rewrite of the bytes fed to yajl2_c (module docstring, "Tokens yajl2_c
    mishandles"): an integer token of more than :data:`SAFE_INT_DIGITS` digits gets the
    exponent ``e0``; a lone low surrogate escape (one no high escape right before it takes --
    yajl2_c pairs a high escape with whatever ``\\u`` escape follows) becomes ``\\u003f``.

    The state describes the end of the data returned so far: inside a string or not (the
    parity of the quotes that no odd run of backslashes escapes -- exact for valid JSON, and
    yajl2_c stops at the first invalid byte before anything behind it matters), the run of
    backslashes there, an integer token still open there (its digits so far), whether an
    unpaired high escape ends there, and its last two bytes. A partial ``\\u`` escape at the
    end of a chunk is held back and sent with the next one."""

    __slots__ = ("bs", "held", "high", "in_str", "prev", "run")

    def __init__(self) -> None:
        self.in_str = False
        self.bs = 0
        self.run = 0
        self.high = False
        self.prev = b""
        self.held = b""

    # -- lexical questions about the chunk ``c`` (state: its start) ---------------------
    def _escape_start(self, c: bytes, i: int) -> bool:
        """The backslash ``c[i]`` starts an escape (an even run of backslashes before it)."""
        j = i
        while j and c[j - 1] == 0x5C:
            j -= 1
        run = i - j + (self.bs if j == 0 else 0)
        return not run & 1

    def _escaped_quotes(self, c: bytes, end: int) -> int:
        """Quotes in ``c[:end]`` after an odd run of backslashes: ``N1 - N2 + N3 - ...`` where
        ``Nk`` counts the quotes after at least ``k`` backslashes (C-speed counts)."""
        total = 0
        if c.find(b"\\", 0, end) >= 0:
            pat = b'\\"'
            sign = 1
            while True:
                k = c.count(pat, 0, end)
                if not k:
                    break
                total += sign * k
                sign = -sign
                pat = b"\\" + pat
        if self.bs & 1:
            # the backslashes at the start of c continue an odd run: the first quote after
            # them changes sides
            j = 0
            while j < end and c[j] == 0x5C:
                j += 1
            if j < end and c[j] == 0x22:
                total += -1 if j & 1 else 1
        return total

    def _in_string(self, c: bytes, i: int) -> bool:
        q = c.count(b'"', 0, i)
        if q:
            q -= self._escaped_quotes(c, i)
        return self.in_str ^ bool(q & 1)

    def _integer_part(self, c: bytes, s: int) -> bool:
        """The digit run starting at ``c[s]`` (outside strings) is a number's integer part
        (not its fraction or exponent)."""
        ctx = (self.prev + c[max(0, s - 2):s])[-2:]
        if not ctx:
            return True
        if ctx[-1] == 0x2D:   # "-": the number's sign, or its exponent's
            return len(ctx) < 2 or ctx[-2] not in b"eE"
        return ctx[-1] not in b".eE+"

    def _unconsumed_high(self, c: bytes, q: int) -> bool:
        """The escape ending at ``c[q]`` is a high surrogate escape that takes the ``\\u``
        escape after it (it was not taken itself by a high escape right before it)."""
        k = 0
        while q >= 6 and _HIGH_ESCAPE.match(c, q - 6, q) is not None and self._escape_start(c, q - 6):
            k += 1
            q -= 6
        base = self.high if q == 0 else False   # an unpaired high escape right before the first one
        if not k:
            return base
        # along a chain of high escapes, every other one is taken by the one before it
        return base if (k - 1) & 1 else not base

    def feed(self, chunk: bytes, final: bool = False) -> bytes:
        """The bytes to send for ``chunk`` (``final``: nothing follows -- flush)."""
        c = self.held + chunk if self.held else chunk
        self.held = b""
        if not final and c:
            i = c.rfind(b"\\", max(0, len(c) - 5))
            if i >= 0 and _PARTIAL_ESCAPE.fullmatch(c, i + 1) is not None and self._escape_start(c, i):
                c, self.held = c[:i], c[i:]
        n = len(c)
        edits: list[tuple[int, int, bytes]] = []
        start = 0
        if self.prev[-1:].isdigit():
            # c's leading digits continue the digit run the data before ends with: the open
            # integer token (``run``), or a run that is not one (a fraction, an exponent, digits
            # in a string) -- never a run of their own
            lead = n - len(c.lstrip(_DIGITS))
            if self.run:
                if lead == n:
                    self.run += n   # the open integer token goes on (no quote, no escape in c)
                else:
                    if self.run + lead > SAFE_INT_DIGITS and c[lead] not in b".eE":
                        edits.append((lead, lead, b"e0"))
                    self.run = 0
            start = lead
        if start < n:
            t = c.translate(_DIGIT_MAP)
            i = t.find(_LONG_RUN, start)
            while i >= 0:
                e = t.find(b"\x01", i)
                if e < 0:
                    break           # the run reaches the end: below, with the next chunk
                if c[e] not in b".eE" and self._integer_part(c, i) and not self._in_string(c, i):
                    edits.append((e, e, b"e0"))
                i = t.find(_LONG_RUN, e)
            if c.find(b"\\u", start) >= 0:
                for m in _LOW_ESCAPE.finditer(c, start):
                    p = m.start()
                    if self._escape_start(c, p) and not self._unconsumed_high(c, p):
                        edits.append((p, p + 6, _QUESTION_ESCAPE))
            q = c.count(b'"')
            if q:
                q -= self._escaped_quotes(c, n)
            end_in_str = self.in_str ^ bool(q & 1)
            tail = n - len(c.rstrip(_DIGITS)) if 0x30 <= c[-1] <= 0x39 else 0
            if tail and not end_in_str and self._integer_part(c, n - tail):
                self.run = tail     # an integer token may go on in the next chunk
            high = self._unconsumed_high(c, n)
            bs_tail = n - len(c.rstrip(b"\\")) if c[-1] == 0x5C else 0
            self.bs = self.bs + n if bs_tail == n else bs_tail
            self.in_str = end_in_str
            self.high = high
            self.prev = (self.prev + c[-2:])[-2:]
        elif n:
            self.high = False
            self.prev = (self.prev + c[-2:])[-2:]
        if edits:
            edits.sort()
            parts = []
            last = 0
            for a, b, rep in edits:
                parts.append(c[last:a])
                parts.append(rep)
                last = b
            parts.append(c[last:])
            c = b"".join(parts)
        if final and self.run:
            if self.run > SAFE_INT_DIGITS:
                c += b"e0"
            self.run = 0
        return c


def _batches(reader: _RangeReader, careful: bool, cancel: CancelToken | None) -> Iterator[list[tuple[str, Any]]]:
    """The ``ijson.basic_parse`` events (``use_float=False``) of everything ``reader``
    returns, one list per chunk fed (valid until the next one is requested), in adaptive
    chunks (module docstring, "Read size"). Normal mode raises :class:`_Careful` before it
    would feed a digit run longer than :data:`SAFE_INT_DIGITS`; careful mode feeds the chunks
    through :class:`_CarefulFilter` (module docstring, "Tokens yajl2_c mishandles"). ijson
    errors are raised as they are; the events of a chunk that fails are dropped."""
    events = ijson.sendable_list()
    coro = ijson.basic_parse_coro(events, use_float=False)
    filt = _CarefulFilter() if careful else None
    digits = 0                       # normal mode: the digit run at the end of the data sent so far
    size = BUF_SIZE
    open_kind: str | None = None     # the last chunk without events ends in a "string" / "number"
    escaped = 0                      # "string": backslashes at the end of the data sent so far
    left: memoryview | None = None   # read but not sent yet
    while True:
        if left is None:
            raw = reader.read(size)
            if not raw:
                break
            left = memoryview(raw)
        chunk = left[:size]
        if open_kind == "string":
            m = _STRING_REST.match(chunk, escaped & 1)
            if m is not None:
                chunk = chunk[:m.end()]
        elif open_kind == "number":
            m = _NUMBER_END.search(chunk)
            if m is not None:
                chunk = chunk[:m.start() + 1]
        n = len(chunk)
        left = left[n:] if len(left) > n else None
        check(cancel)
        data = bytes(chunk)
        if filt is None:
            digits = _digit_run(data, digits)
        else:
            data = filt.feed(data)
        if data:   # an empty send would end the input
            coro.send(data)
        if events:
            yield events
            del events[:]
            size = BUF_SIZE
            open_kind = None
            continue
        # no event: the chunk lies inside one token, or between tokens
        if open_kind != "string":
            if _OUTSIDE_STRING.fullmatch(chunk) is None:
                open_kind = "string"   # (or a literal such as "tru": the next chunk ends it)
                escaped = 0
            elif chunk[n - 1] in _SEPARATORS:
                open_kind = None       # whitespace is not re-read: no need to grow
                size = BUF_SIZE
                continue
            else:
                open_kind = "number"   # (or a string of digits: re-checked on the next chunk)
        if open_kind == "string":
            k = 0
            while k < n and chunk[n - 1 - k] == 0x5C:
                k += 1
            escaped = escaped + k if k == n else k
        size = min(size * 2, BUF_MAX)
    if filt is not None:
        data = filt.feed(b"", final=True)
        if data:
            coro.send(data)
    coro.close()  # end of input: the last number, or IncompleteJSONError
    if events:
        yield events


def _parse(reader: _RangeReader, cancel: CancelToken | None = None) -> Iterator[tuple[str, Any]]:
    """``ijson.basic_parse`` events (``use_float=False``) of everything ``reader`` returns,
    never feeding yajl2_c a token it mishandles: when normal mode meets such a token, the
    bytes are parsed again from the start (``reader.again()``) in careful mode and the events
    already delivered are skipped (module docstring, "Tokens yajl2_c mishandles"). ``cancel``
    is checked once per chunk."""
    delivered = 0
    try:
        for batch in _batches(reader, False, cancel):
            yield from batch
            delivered += len(batch)
        return
    except _Careful:
        why = "a digit run longer than %d digits" % SAFE_INT_DIGITS
    except UnicodeDecodeError:
        why = "a lone low surrogate escape"
    log.debug("stream: %s; parsing again in careful mode (%d events delivered)", why, delivered)
    skip = delivered
    for batch in _batches(reader.again(), True, cancel):
        if skip >= len(batch):
            skip -= len(batch)
            continue
        if skip:
            yield from batch[skip:]
            skip = 0
        else:
            yield from batch


def _number(v: Any) -> Any:
    """ijson number -> the type codec.loads produces for the same token."""
    if isinstance(v, int):
        if _INT_MIN <= v <= _UINT_MAX:
            return v
        return codec.loads(str(v))
    if isinstance(v, Decimal):
        return codec.loads(str(v))
    return v


def _span(span: Any) -> tuple[int, int]:
    start, end = span
    start, end = int(start), int(end)
    if start < 0 or end < start:
        raise ValueError(f"invalid span ({start}, {end})")
    return start, end


def _open(doc: Any) -> Any:
    """A reader of ``doc``'s virtual address space (``doc.open_data()``: the data file checked
    against the version opened, and the edit layer of an edited document -- docs/ARCHITECTURE.md
    §12.3); a document-like object without ``open_data`` is read from ``doc.data_path``."""
    open_data = getattr(doc, "open_data", None)
    if open_data is not None:
        return open_data()
    return open_shared(doc.data_path, "rb")


def iter_events(doc: Any, span: tuple[int, int], member: bool = False, *,
                cancel: CancelToken | None = None) -> Iterator[tuple[str, Any]]:
    """Raw ``ijson.basic_parse`` events of ``doc``'s data in ``span`` (numbers as ``int``
    / ``Decimal``; an integer token longer than :data:`SAFE_INT_DIGITS` digits arrives as an
    exact ``Decimal`` with exponent 0 -- module docstring, "Tokens yajl2_c mishandles"). With
    ``member=True`` the span is parsed as ``{`` + span + ``}`` (the events then start with
    ``start_map``, ``map_key``). Invalid JSON -- and a number beyond ``Decimal``'s exponent
    range -- raises ``JsonReshapeError("stream.error.parse")``. The span may be an edit offset
    (an edited record): it is read through ``doc.open_data()``. ``cancel`` is checked once per
    chunk read (64 KiB and more)."""
    start, end = _span(span)
    with _open(doc) as f:
        reader = _RangeReader(f, start, end, b"{" if member else b"", b"}" if member else b"")
        try:
            yield from _parse(reader, cancel)
        except (ijson.JSONError, ValueError, SystemError, ArithmeticError) as exc:
            # ArithmeticError: decimal.InvalidOperation of a token beyond Decimal's exponent range
            raise JsonReshapeError("stream.error.parse", detail=str(exc).splitlines()[0] if str(exc) else "") \
                from exc


def _record_events(doc: Any, span: tuple[int, int], member: bool,
                   cancel: CancelToken | None) -> Iterator[tuple[str, Any]]:
    """:func:`_value_events`, with ``cancel=`` only when there is a token: the three-argument
    call of the internal hook stays what it was (tests of other modules replace the hook)."""
    if cancel is None:
        return _value_events(doc, span, member)
    return _value_events(doc, span, member, cancel=cancel)


def _value_events(doc: Any, span: tuple[int, int], member: bool, *,
                  cancel: CancelToken | None = None) -> Iterator[tuple[str, Any]]:
    """Events of the record *value* (the member wrapper removed)."""
    events = iter_events(doc, span, member, cancel=cancel)
    if not member:
        yield from events
        return
    depth = 0
    started = False
    for event, value in events:
        if not started:
            if event == "map_key":
                started = True
            continue
        if depth == 0 and event == "end_map":
            break  # the wrapper's closing brace
        if event in ("start_map", "start_array"):
            depth += 1
        elif event in ("end_map", "end_array"):
            depth -= 1
        yield event, value


def _event_cost(event: str, value: Any) -> int:
    """Estimated bytes of keeping the value of one event (module docstring)."""
    if event == "string":
        return _COST_STR + len(value)
    if event == "map_key":
        return _COST_KEY + len(value)
    if event == "number":
        return _COST_SCALAR
    if event == "start_map" or event == "start_array":
        return _COST_CONTAINER
    if event == "boolean" or event == "null":
        return _COST_LITERAL
    return 0


# ----------------------------------------------------------------------------------
# whole texts: what yajl2_c mishandles, and the exact pure-Python parser
# ----------------------------------------------------------------------------------
def needs_exact(data: bytes | bytearray | memoryview) -> bool:
    """True when ijson's yajl2_c backend would mishandle the JSON text ``data`` (module
    docstring, "Tokens yajl2_c mishandles"): it holds a digit run longer than
    :data:`SAFE_INT_DIGITS` (perhaps an integer it cannot convert -- that crashes the process),
    or a surrogate escape that is not part of a plain pair (yajl2_c reads a lone high one as
    ``"?"`` and fails on a lone low one). Digit runs and escapes inside strings count too (the
    check does not tokenize). A few C-speed scans: ~1 ms per MiB."""
    buf = data if isinstance(data, bytes) else bytes(data)
    if buf.translate(_DIGIT_MAP).find(_LONG_RUN) >= 0:
        return True
    if buf.find(b"\\u") < 0:
        return False
    n = len(_SURROGATE_ESCAPE.findall(buf))
    if not n:
        return False
    return n != 2 * len(_SURROGATE_PAIR.findall(buf)) or _SURROGATE_DOUBT.search(buf) is not None


_EX_WS = re.compile(r"[ \t\n\r]*")
_EX_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")
_EX_NUMBER_NEXT = frozenset(".eE+-0123456789")   # a complete number token is never followed by these
_EX_LITERALS = (("true", "boolean", True), ("false", "boolean", False), ("null", "null", None))
_scanstring = json.decoder.scanstring


def _exact_number(tok: str) -> Any:
    """``codec.loads(tok)`` for one number token; a token it refuses (only possible when the
    int-digit limit was lowered below ``codec``'s assumption) keeps its text as a ``JsonFloat``."""
    try:
        return codec.loads(tok)
    except (JsonReshapeError, ValueError):
        return codec.JsonFloat(tok)


def exact_events(data: bytes | bytearray | memoryview | str, *, convert: bool = True) -> Iterator[tuple[str, Any]]:
    """``ijson.basic_parse``-style events of the JSON text ``data``, decoded exactly like
    :func:`jsonreshape.codec.loads` -- keys and strings keep lone surrogates, numbers are what
    ``codec.loads`` gives the token (``convert=False``: the token's text) -- by a pure-Python
    parser: no ijson (module docstring, "Tokens yajl2_c mishandles"), no recursion (any depth),
    ~5-10 MB/s. Invalid or truncated JSON yields the events up to the problem, then raises
    ``ijson.IncompleteJSONError`` (a number cut by the end of the text counts as complete, as
    with ijson); bytes are UTF-8 and the events end at the first byte that is not."""
    if isinstance(data, str):
        text = data
    else:
        raw = bytes(data)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            text = raw[:exc.start].decode("utf-8")
    n = len(text)
    ws = _EX_WS.match
    stack: list[bool] = []            # True: an object
    pos = ws(text, 0).end()
    state = 0                         # 0 a value, 1 a value or "]", 2 a key, 3 a key or "}", 4 after a value

    def fail(what: str) -> ijson.IncompleteJSONError:
        return ijson.IncompleteJSONError(f"{what} at character {pos}")

    while True:
        if state == 4:
            if not stack:
                if pos < n:
                    raise fail("trailing data")
                return
            if pos >= n:
                raise fail("premature end")
            ch = text[pos]
            if ch == ",":
                state = 2 if stack[-1] else 0
            elif ch == ("}" if stack[-1] else "]"):
                yield ("end_map" if stack.pop() else "end_array"), None
            else:
                raise fail("unexpected character")
            pos = ws(text, pos + 1).end()
            continue
        if pos >= n:
            raise fail("premature end")
        ch = text[pos]
        if state >= 2:
            if ch == '"':
                try:
                    key, pos = _scanstring(text, pos + 1, True)
                except ValueError:
                    raise fail("bad string") from None
                yield "map_key", key
                pos = ws(text, pos).end()
                if pos >= n or text[pos] != ":":
                    raise fail("colon expected")
                pos = ws(text, pos + 1).end()
                state = 0
                continue
            if ch == "}" and state == 3:
                stack.pop()
                yield "end_map", None
                pos = ws(text, pos + 1).end()
                state = 4
                continue
            raise fail("key expected")
        if ch == "{":
            yield "start_map", None
            stack.append(True)
            state = 3
            pos = ws(text, pos + 1).end()
            continue
        if ch == "[":
            yield "start_array", None
            stack.append(False)
            state = 1
            pos = ws(text, pos + 1).end()
            continue
        if ch == "]" and state == 1:
            stack.pop()
            yield "end_array", None
            state = 4
            pos = ws(text, pos + 1).end()
            continue
        state = 4
        if ch == '"':
            try:
                s, end = _scanstring(text, pos + 1, True)
            except ValueError:
                raise fail("bad string") from None
            yield "string", s
            pos = ws(text, end).end()
            continue
        m = _EX_NUMBER.match(text, pos)
        if m is not None:
            end = m.end()
            if end < n and text[end] in _EX_NUMBER_NEXT:
                raise fail("bad number")
            tok = m.group()
            yield "number", (_exact_number(tok) if convert else tok)
            pos = ws(text, end).end()
            continue
        for word, event, value in _EX_LITERALS:
            if text.startswith(word, pos):
                yield event, value
                pos = ws(text, pos + len(word)).end()
                break
        else:
            raise fail("value expected")


# ----------------------------------------------------------------------------------
# pattern matching (an NFA per pattern, like PathPattern._resolve_walk)
# ----------------------------------------------------------------------------------
class _Matcher:
    __slots__ = ("segs", "n", "_closures", "_steps", "_ints")

    def __init__(self, pattern: PathPattern) -> None:
        self.segs = pattern.segments
        self.n = len(self.segs)
        self._closures: dict[frozenset[int], frozenset[int]] = {}
        self._steps: dict[tuple[frozenset[int], Any], frozenset[int]] = {}
        # array positions the pattern names explicitly; any other position steps alike
        ints: set[int] = set()
        for s in self.segs:
            if isinstance(s, int):
                ints.add(s)
            elif isinstance(s, str):
                ci = canonical_index(s)
                if ci is not None:
                    ints.add(ci)
        self._ints = frozenset(ints)

    def closure(self, states: frozenset[int]) -> frozenset[int]:
        cached = self._closures.get(states)
        if cached is None:
            out = set(states)
            for i in states:
                j = i
                while j < self.n and self.segs[j] is Wild.DESCENDANTS:
                    j += 1
                    out.add(j)
            cached = self._closures[states] = frozenset(out)
        return cached

    def initial(self) -> frozenset[int]:
        return self.closure(frozenset({0}))

    def step(self, states: frozenset[int], seg: str | int) -> frozenset[int]:
        nxt = set()
        for i in states:
            if i == self.n:
                continue
            pseg = self.segs[i]
            if pseg is Wild.DESCENDANTS:
                nxt.add(i)
            elif pseg is Wild.ANY_INDEX:
                if isinstance(seg, int):
                    nxt.add(i + 1)
            elif pseg is Wild.ANY_KEY:
                if isinstance(seg, str):
                    nxt.add(i + 1)
            elif isinstance(pseg, int):
                if isinstance(seg, int) and seg == pseg:
                    nxt.add(i + 1)
            elif isinstance(seg, int):
                if canonical_index(pseg) == seg:
                    nxt.add(i + 1)
            elif seg == pseg:
                nxt.add(i + 1)
        return self.closure(frozenset(nxt)) if nxt else frozenset()

    def step_cached(self, states: frozenset[int], seg: str | int) -> frozenset[int]:
        """:meth:`step` with a bounded cache (array positions the pattern does not name
        share one entry)."""
        key_seg: Any = seg
        if isinstance(seg, int) and seg not in self._ints:
            key_seg = -1  # never a real position
        key = (states, key_seg)
        res = self._steps.get(key)
        if res is None:
            res = self.step(states, seg)
            if len(self._steps) >= _STEP_CACHE_MAX:
                self._steps.clear()
            self._steps[key] = res
        return res


def _normalize_patterns(patterns: Iterable[PathPattern | str]) -> list[PathPattern]:
    out = []
    for p in patterns:
        out.append(p if isinstance(p, PathPattern) else parse_pattern(str(p)))
    return out


class _Budget:
    __slots__ = ("used", "limit")

    def __init__(self, limit: int | None) -> None:
        self.used = 0
        self.limit = limit

    def add(self, n: int) -> None:
        self.used += n
        if self.limit is not None and self.used > self.limit:
            raise CaptureLimitExceeded(limit=format_bytes(self.limit))


class _Dup(Exception):
    """Internal: a duplicate key on a live path (the fast scan cannot place it)."""


# ----------------------------------------------------------------------------------
# the fast scan (no positions kept; restarts on duplicate keys)
# ----------------------------------------------------------------------------------
def _scan(doc: Any, span: tuple[int, int], pats: list[PathPattern], member: bool, capture: list[bool],
          probes: list[list[Any]], budget: _Budget, first_only: bool, cancel: CancelToken | None) -> list[list[Any]]:
    """One pass over the record: captured values per pattern (document order) and probe
    callbacks. Raises :class:`_Dup` on a duplicate key on a live path."""
    matchers = [_Matcher(p) for p in pats]
    npat = len(pats)
    finals = [m.n for m in matchers]
    out: list[list[Any]] = [[] for _ in range(npat)]
    started = [False] * npat               # first_only: a value (or its builder) exists
    builders: list[list[Any]] = []         # [ObjectBuilder, depth, [(pattern, slot), ...]]
    # frame: [is_map, states|None, keys|None, count, watchers|None, key, cost of keys]
    stack: list[list[Any]] = []
    root_states: list[frozenset[int]] | None = [m.initial() for m in matchers]
    steps = [m.step_cached for m in matchers]
    rng = range(npat)
    n_events = 0
    add = budget.add
    for event, value in _record_events(doc, span, member, cancel):
        n_events += 1
        if n_events & (CHECK_EVENTS - 1) == 0:
            check(cancel)
        if event == "map_key":
            if builders:
                cost = _COST_KEY + len(value)
                for b in builders:
                    b[0].event(event, value)
                    add(cost)
            top = stack[-1]
            top[5] = value
            watchers = top[4]
            if watchers:
                for p in watchers:
                    p.child("key", value)
            continue
        if event == "end_map" or event == "end_array":
            depth = len(stack)
            frame = stack.pop()
            if frame[6]:
                budget.used -= frame[6]  # the duplicate-key bookkeeping of a closed object
            if builders:
                for b in builders:
                    b[0].event(event, value)
                while builders and builders[-1][1] == depth:
                    b = builders.pop()
                    for k, slot in b[2]:
                        out[k][slot] = b[0].value
            watchers = frame[4]
            if watchers:
                for p in watchers:
                    p.close()
            continue
        # a value starts: its position and pattern states
        is_container = event == "start_map" or event == "start_array"
        if stack:
            top = stack[-1]
            pstates = top[1]
            if top[0]:
                seg: Any = top[5]
            else:
                seg = top[3]
                top[3] += 1
                watchers = top[4]
                if watchers:
                    if is_container:
                        for p in watchers:
                            p.child("container", None)
                    else:
                        cv = _number(value) if event == "number" else value
                        for p in watchers:
                            p.child("value", cv)
            if pstates is None:
                states = None
            else:
                states = []
                alive = False
                for k in rng:
                    st = pstates[k]
                    nxt = steps[k](st, seg) if st else st
                    states.append(nxt)
                    if nxt:
                        alive = True
                if not alive:
                    states = None
                elif top[0]:
                    keys = top[2]
                    if seg in keys:
                        raise _Dup()
                    keys.add(seg)
                    cost = _COST_KEY + len(seg)
                    top[6] += cost
                    add(cost)
        else:
            states = root_states
        if event == "number" and (states is not None or builders):
            value = _number(value)
        if builders:
            cost = _event_cost(event, value)
            for b in builders:
                b[0].event(event, value)
                add(cost)
        if states is not None:
            hits = [k for k in rng if finals[k] in states[k]]
        else:
            hits = ()
        if is_container:
            is_map = event == "start_map"
            watching: list[Any] | None = None
            if hits:
                new_rows: list[tuple[int, int]] = []
                for k in hits:
                    if capture[k] and not (first_only and started[k]):
                        started[k] = True
                        out[k].append(None)
                        new_rows.append((k, len(out[k]) - 1))
                    for p in probes[k]:
                        if p.open(is_map):
                            if watching is None:
                                watching = []
                            watching.append(p)
                if new_rows:
                    b = ijson.ObjectBuilder()
                    b.event(event, value)
                    add(_COST_CONTAINER)
                    builders.append([b, len(stack) + 1, new_rows])
            stack.append([is_map, states, set() if (is_map and states is not None) else None, 0, watching, None, 0])
        elif hits:
            for k in hits:
                if capture[k] and not (first_only and started[k]):
                    started[k] = True
                    add(_event_cost(event, value) + 8)  # before keeping it (may raise)
                    out[k].append(value)
                for p in probes[k]:
                    p.value(value)
    return out


# ----------------------------------------------------------------------------------
# the exact scan (positions kept: dict semantics for duplicate keys)
# ----------------------------------------------------------------------------------
class _Frame:
    __slots__ = ("is_map", "path", "sortkey", "states", "key", "count", "keys", "cost")

    def __init__(self, is_map: bool, path: tuple, sortkey: tuple, states: list[frozenset[int]] | None) -> None:
        self.is_map = is_map
        self.path = path
        self.sortkey = sortkey
        self.states = states        # None: no pattern can match below (dead subtree)
        self.key: str | None = None
        self.count = 0
        self.keys: dict[str, int] | None = {} if (is_map and states is not None) else None
        self.cost = 0               # estimated bytes of ``keys``


def _values_exact(doc: Any, span: tuple[int, int], pats: list[PathPattern], member: bool, budget: _Budget,
                  first_only: bool, cancel: CancelToken | None) -> dict[int, list[Any]]:
    """:func:`stream_values` for records with duplicate keys: every capture remembers its
    path and document position, a duplicate key drops the captures of the replaced
    member, and the captures are finally ordered by position (dict semantics)."""
    matchers = [_Matcher(p) for p in pats]
    npat = len(pats)
    # captures: [pattern index, path, sortkey, value, cost]
    captures: list[list[Any]] = []
    builders: list[tuple[ijson.ObjectBuilder, int, list[list[Any]]]] = []   # (builder, depth, capture rows)
    stack: list[_Frame] = []
    root_states = [m.initial() for m in matchers]
    add = budget.add

    def child_position() -> tuple[tuple, tuple, list[frozenset[int]] | None]:
        """(path, sortkey, states) of the value starting now."""
        if not stack:
            return (), (), root_states
        top = stack[-1]
        if top.is_map:
            seg: str | int = top.key  # type: ignore[assignment]
            ordinal = 0
            if top.keys is not None:
                prev = top.keys.get(seg)  # type: ignore[arg-type]
                if prev is None:
                    ordinal = top.keys[seg] = len(top.keys)  # type: ignore[index]
                    cost = _COST_KEY + len(seg)  # type: ignore[arg-type]
                    top.cost += cost
                    add(cost)
                else:
                    ordinal = prev
                    _drop_below(top.path + (seg,))
        else:
            seg = top.count
            ordinal = top.count
            top.count += 1
        if top.states is None:
            return top.path + (seg,), top.sortkey + (ordinal,), None
        states = []
        alive = False
        for k in range(npat):
            st = top.states[k]
            nxt = matchers[k].step_cached(st, seg) if st else frozenset()
            states.append(nxt)
            alive = alive or bool(nxt)
        return top.path + (seg,), top.sortkey + (ordinal,), states if alive else None

    def _drop_below(prefix: tuple) -> None:
        """A duplicate key replaces the earlier member: forget its captures."""
        n = len(prefix)
        kept = []
        for c in captures:
            if c[1][:n] == prefix:
                budget.used -= c[4]
            else:
                kept.append(c)
        captures[:] = kept

    def matched(states: list[frozenset[int]] | None) -> list[int]:
        if states is None:
            return []
        return [k for k in range(npat) if matchers[k].n in states[k]]

    n_events = 0
    for event, value in _record_events(doc, span, member, cancel):
        n_events += 1
        if n_events & (CHECK_EVENTS - 1) == 0:
            check(cancel)
        if event == "map_key":
            for b, _, rows in builders:
                b.event(event, value)
                cost = _COST_KEY + len(value)
                for row in rows:
                    row[4] += cost
                add(cost)
            stack[-1].key = value
            continue
        if event in ("end_map", "end_array"):
            depth = len(stack)
            budget.used -= stack.pop().cost
            for b, _, _ in builders:
                b.event(event, value)
            while builders and builders[-1][1] == depth:
                b, _, rows = builders.pop()
                for row in rows:
                    row[3] = b.value
            continue
        if event == "number":
            value = _number(value)
        path, sortkey, states = child_position()
        hits = matched(states)
        if builders:
            cost = _event_cost(event, value)
            for b, _, rows in builders:
                b.event(event, value)
                for row in rows:
                    row[4] += cost
                add(cost)
        if event in ("start_map", "start_array"):
            stack.append(_Frame(event == "start_map", path, sortkey, states))
            if hits:
                b = ijson.ObjectBuilder()
                b.event(event, value)
                rows = [[k, path, sortkey, None, _COST_CONTAINER + 8 * len(path)] for k in hits]
                captures.extend(rows)
                builders.append((b, len(stack), rows))
                add(sum(r[4] for r in rows))
        elif hits:
            cost = _event_cost(event, value) + 8 + 16 * len(path)
            for k in hits:
                captures.append([k, path, sortkey, value, cost])
                add(cost)
    out: dict[int, list[Any]] = {k: [] for k in range(npat)}
    captures.sort(key=lambda c: c[2])
    for k, _, _, v, _ in captures:
        if not (first_only and out[k]):
            out[k].append(v)
    return out


# ----------------------------------------------------------------------------------
# public entry points
# ----------------------------------------------------------------------------------
def stream_values(doc: Any, span: tuple[int, int], patterns: Iterable[PathPattern | str],
                  member: bool = False, *, max_bytes: int | None = None, first_only: bool = False,
                  cancel: CancelToken | None = None) -> dict[int, list[Any]]:
    """Values of each pattern inside the record ``span`` (position in ``patterns`` ->
    values in document order), equal to ``[p.values(codec.loads(record)) for p in
    patterns]`` (dict semantics); with ``first_only=True`` at most the first value of
    each. Containers matched by a pattern are built in memory (ijson ``ObjectBuilder``);
    everything else is only streamed. ``max_bytes``: budget of the captured data
    (module docstring) -- :class:`CaptureLimitExceeded` when it would be exceeded.
    ``cancel`` is checked every :data:`CHECK_EVENTS` events."""
    pats = _normalize_patterns(patterns)
    npat = len(pats)
    if max_bytes is not None and max_bytes < 0:
        raise ValueError(f"max_bytes must be >= 0, got {max_bytes!r}")
    got: list[list[Any]] | None
    try:
        got = _scan(doc, span, pats, member, [True] * npat, [[] for _ in range(npat)], _Budget(max_bytes),
                    first_only, cancel)
    except _Dup:
        got = None  # the second pass runs after the first one's values are released
    if got is None:
        log.debug("stream_values: duplicate keys in the record at %s; streaming it again with positions", span)
        return _values_exact(doc, span, pats, member, _Budget(max_bytes), first_only, cancel)
    return {k: got[k] for k in range(npat)}


def stream_probe(doc: Any, span: tuple[int, int], patterns: Iterable[PathPattern | str],
                 probes: dict[int, list[Any]], member: bool = False, *, capture: Iterable[int] = (),
                 max_bytes: int | None = None, cancel: CancelToken | None = None) -> dict[int, list[Any]]:
    """One pass over the record ``span`` feeding, for each pattern index ``k``, the nodes
    pattern ``k`` matches to the objects in ``probes[k]`` (the :class:`StreamProbe`
    protocol), and capturing the values of the pattern indices in ``capture`` (returned
    like :func:`stream_values`, within ``max_bytes``). Probed values are not kept.

    Raises :class:`DuplicateKeys` when a duplicate key appears on a path a pattern can
    still match (evaluate the record from :func:`stream_values` then) and
    :class:`CaptureLimitExceeded` when the captured values exceed ``max_bytes``."""
    pats = _normalize_patterns(patterns)
    npat = len(pats)
    cap = set(capture)
    lists = [list(probes.get(k, ())) for k in range(npat)]
    got: list[list[Any]] | None
    try:
        got = _scan(doc, span, pats, member, [k in cap for k in range(npat)], lists, _Budget(max_bytes), False,
                    cancel)
    except _Dup:
        got = None  # raised below, once the pass's values are released
    if got is None:
        raise DuplicateKeys()
    return {k: got[k] for k in sorted(cap)}


def stream_text_prefix(doc: Any, span: tuple[int, int], pattern: PathPattern | str, member: bool = False, *,
                       limit: int, cancel: CancelToken | None = None) -> tuple[str, Any, bool] | None:
    """The beginning of the first value ``pattern`` matches in the record (document
    order; duplicate keys are not collapsed), without keeping more than ``limit`` bytes:

    * ``("object" | "array", text, complete)``: ``text`` = the first ``limit`` bytes of
      the value's compact JSON text (what :func:`jsonreshape.codec.dumps` writes, with the
      ijson deviations of this module), ``complete`` = the whole text fits;
    * ``("string", s, complete)``: the first ``limit`` characters of a string;
    * ``("scalar", v, True)``: any other value;
    * ``None`` when nothing matches.

    Reading stops as soon as the prefix is known (the sort keys of values too large to
    capture use it)."""
    pat = pattern if isinstance(pattern, PathPattern) else parse_pattern(str(pattern))
    matcher = _Matcher(pat)
    final = matcher.n
    stack: list[list[Any]] = []   # [is_map, state|None, key, count]
    root = matcher.initial()
    n_events = 0
    events = _record_events(doc, span, member, cancel)
    try:
        for event, value in events:
            n_events += 1
            if n_events & (CHECK_EVENTS - 1) == 0:
                check(cancel)
            if event == "map_key":
                stack[-1][2] = value
                continue
            if event == "end_map" or event == "end_array":
                stack.pop()
                continue
            if stack:
                top = stack[-1]
                if top[0]:
                    seg: Any = top[2]
                else:
                    seg = top[3]
                    top[3] += 1
                st = top[1]
                state = matcher.step_cached(st, seg) if st else None
            else:
                state = root
            if state and final in state:
                if event == "start_map" or event == "start_array":
                    text, complete = _compact_prefix(event, events, limit, cancel)
                    return ("object" if event == "start_map" else "array"), text, complete
                if event == "string":
                    return "string", value[:limit], len(value) <= limit
                return "scalar", _number(value) if event == "number" else value, True
            if event == "start_map" or event == "start_array":
                stack.append([event == "start_map", state or None, None, 0])
    finally:
        events.close()
    return None


_LITERAL_TEXT = {True: b"true", False: b"false", None: b"null"}


def _compact_prefix(first_event: str, events: Iterator[tuple[str, Any]], limit: int,
                    cancel: CancelToken | None) -> tuple[bytes, bool]:
    """Compact JSON text of the container whose start event was just read, from the
    following events, cut at ``limit`` bytes (``complete`` = it fits)."""
    out = bytearray(b"{" if first_event == "start_map" else b"[")
    stack: list[list[Any]] = [[first_event == "start_map", 0]]   # [is_map, members/elements so far]
    dumps = codec.dumps
    n_events = 0
    for event, value in events:
        n_events += 1
        if n_events & (CHECK_EVENTS - 1) == 0:
            check(cancel)
        if len(out) > limit:
            return bytes(out[:limit]), False
        if event == "map_key":
            top = stack[-1]
            if top[1]:
                out += b","
            top[1] += 1
            out += dumps(value)
            out += b":"
            continue
        if event == "end_map" or event == "end_array":
            stack.pop()
            out += b"}" if event == "end_map" else b"]"
            if not stack:
                break
            continue
        top = stack[-1]
        if not top[0]:
            if top[1]:
                out += b","
            top[1] += 1
        if event == "start_map" or event == "start_array":
            out += b"{" if event == "start_map" else b"["
            stack.append([event == "start_map", 0])
        elif event == "string":
            out += dumps(value)
        elif event == "number":
            out += codec.number_text(_number(value)).encode("ascii")
        else:
            out += _LITERAL_TEXT[value]
    if len(out) > limit:
        return bytes(out[:limit]), False
    return bytes(out), True


def stream_walk(doc: Any, span: tuple[int, int], member: bool = False, *,
                cancel: CancelToken | None = None) -> Iterator[tuple[tuple, str, Any]]:
    """Every object key and scalar of the record in document order: ``(path, "key",
    key)`` (``path`` = the member's path, i.e. ending with the key) and ``(path,
    "value", scalar)``. Paths are relative to the record value."""
    stack: list[list[Any]] = []   # [is_map, path, key, count]
    for event, value in _record_events(doc, span, member, cancel):
        if event == "map_key":
            top = stack[-1]
            top[2] = value
            yield top[1] + (value,), "key", value
            continue
        if event in ("end_map", "end_array"):
            stack.pop()
            continue
        if not stack:
            path: tuple = ()
        else:
            top = stack[-1]
            if top[0]:
                path = top[1] + (top[2],)
            else:
                path = top[1] + (top[3],)
                top[3] += 1
        if event == "start_map":
            stack.append([True, path, None, 0])
        elif event == "start_array":
            stack.append([False, path, None, 0])
        else:
            yield path, "value", _number(value) if event == "number" else value
