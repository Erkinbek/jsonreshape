"""Full-file search runner (docs/ARCHITECTURE.md §7; AGENT_PROMPT §6.1-6.2, §12, §14 tests 15-16).

:func:`search` finds every object key and scalar value of a document (or of a record
source such as a RecordSet) that matches a :class:`~jsonreshape.textmatch.TextQuery`, and
reports each one as a :class:`SearchHit` (record number, path, key/value, highlighted
snippet). Results stream to ``on_hits`` while the scan runs; the matching records form a
RecordSet ("Natijalar ro'yxati RecordSet sifatida ham ochiladi").

What is matched (the *confirm* step)
====================================
One function, :meth:`_Matcher.match`, decides the hits of a parsed record; both
:func:`search` and the brute-force :func:`reference_search` use it, so the two can be
compared hit for hit (:meth:`_Matcher.match_events` applies the same per-item tests to the
events of a streamed record). It walks the record (``codec.loads``; member records: the member
value, plus the member key) in document order, pre-order:

* member records (keyed documents, wrapped documents whose record container is an
  object): first the record's own key, as a key at path ``()``;
* every object member: its key (at the member's path, e.g. ``("contact", "city")``), then
  its value recursively; array elements in order;
* every scalar value through :func:`jsonreshape.codec.value_text`: strings as they are,
  numbers by their exact JSON text (``1.10``, ``1e400``), ``true``/``false``/``null``.

Plain and regex mode test keys and scalar texts with :meth:`TextQuery.find_spans`; number
mode tests only JSON numbers (never keys, strings or literals) with
:meth:`TextQuery.match_number`. ``scope`` restricts the hits to keys or to values. A path
filter (``SearchOptions.path``, any :func:`jsonreshape.paths.parse_pattern` syntax) keeps a
node when ``pattern.matches_prefix_of(node_path)``: ``"name"`` is the ``name`` field and
everything inside it, ``"**.name"`` / ``"..name"`` a ``name`` at any depth,
``"orders[*].sum"`` every order sum. The record key (path ``()``) passes only a filter that
matches the empty path (``"$"``, ``"**"``), i.e. when there is effectively no filter. Paths
are relative to the record value; a key hit's path ends with that key.

One hit = one matched key or value string (all its match spans are reported). Duplicate
keys follow dict semantics like everywhere else (``codec.loads``: the last value, at the
position of the first key).

Hit text: the whole string when it has at most :data:`SNIPPET_MAX` (120) characters,
otherwise a window from :data:`SNIPPET_CONTEXT` (50) characters before the first match to
50 characters after it, ending at most :data:`SNIPPET_REACH` (150) characters after the
start of the first match (a very long match is cut), with ``"…"`` marking cut ends.
``spans`` are the match spans inside that text (clipped to the window). For a longer
string only the spans such a window can show are computed (``TextQuery.find_spans(s,
reach=SNIPPET_REACH)``), so a string with millions of matches costs no memory; strings
longer than :data:`jsonreshape.textmatch.HUGE_TEXT` characters are matched window by
window without a folded copy of the whole string.

Only records are searched: the wrapper around the record array of a wrapped document
(``{"meta": .., "data": [records]}``) has no record number and yields no hits.

Shortcuts that never change the result (the tests compare with :func:`reference_search`
and with a naive walk calling ``find_spans`` on every string): a key's result is cached
per search; before ``find_spans`` a string must contain the longest apostrophe-free part of
the folded needle (``part in s.casefold()``; with ``translit`` the part of the needle that
folds the same in every context, tested on ASCII strings only, whose transliterated
folding is :func:`jsonreshape.textmatch.translit_fold_ascii` -- no Cyrillic letters, only
the optional apostrophe between ``s`` and ``h`` and ``ts`` -> ``s``; strings longer than
:data:`jsonreshape.textmatch.HUGE_TEXT` skip this test in streamed records, it would fold a
copy of them); numbers and ``true``/``false``/``null`` are skipped when the needle has
a character no number text can contain; and when no number can be a hit the record is
decoded with plain ``orjson.loads`` (only number values may differ from ``codec.loads``,
and they are not looked at; anything orjson refuses goes to ``codec.loads``).

Byte mode and parse mode
========================
When the index is complete (the file's string flags are final),
:meth:`TextQuery.prefilter_needles` (``doc.flags``) gives byte needles: every block of
records goes through ``core.MultiFinder.find_in_spans`` and only the candidate records are
confirmed (``mode_used == "bytes"``; AGENT_PROMPT §12: >= 100 MB/s). Regex, number and
Latin<->Cyrillic searches and files with ``\\uXXXX`` escapes have no needles: every
record is parsed (``mode_used == "parse"``).

*The locator* (byte mode, Rust core: :data:`LOCATE`). A candidate is not parsed:
``core.MultiFinder.locate_tokens`` (per block; the first :data:`LOCATE_HEAD` candidates on
their own, so that the first hits show at once) validates the candidates and reports, in
document order, every key / string / number / literal token of each candidate in which a
needle occurrence starts, with its path (docs/ARCHITECTURE.md §1.5). The needles are sound
per string -- every key or scalar that is a hit holds a needle in its JSON text
(:meth:`TextQuery.prefilter_needles`) -- so a record's hits are among its located tokens;
:meth:`_Matcher.locate_records` decodes only those (strings with ``orjson``, else
``codec.loads``; numbers and literals are their own text, which is what ``codec.loads`` +
``number_text`` gives) and applies the per-item tests of the confirm step above, in
document order -- pre-order, as long as no key repeats. The confirm step (parse and walk)
still handles: records with duplicate keys anywhere (dict semantics; the core flags
them), candidates larger than :data:`INCREMENTAL_BYTES` (their hits come incrementally),
and records with a located number that could be a hit when the record is longer than
:data:`LOCATE_NUMBER_BYTES` (such a record could be nested deeper than ``codec.loads``
accepts; the streaming fallback of the confirm step reads numbers differently, see below).
Strings and keys of such a record cannot differ: the streaming walk reads them like
``codec.loads`` except lone surrogate escapes, and byte mode never sees a ``\\u`` escape
(duplicate keys go to the confirm step anyway). Results of located value tokens are cached
per raw token (:data:`VALUE_CACHE_MAX` entries of at most :data:`VALUE_CACHE_TOKEN` bytes),
path-filter results per path. Once the stored hits are complete (``max_hits``, memory
budget), the located records are only counted (no hit tuples are built). With the
pure-Python core, whose tokenizer is slower than ``orjson``, every candidate is parsed
and walked instead (identical results; the tests run both ways on both cores).

A whole-document search started while
the index is still being built (a background build is started when none is running)
follows the growing index in parse mode, so the first results appear at once; a query
that can use byte needles does so for :data:`FOLLOW_SECONDS` only, then waits for the
index -- which builds many times faster without a parse-mode search competing for the
GIL -- and searches the remaining records in byte mode. A search over an explicit
``source`` waits for the index first. ``mode_used`` is ``"bytes"`` when the prefilter
served at least one block.

Huge records (larger than ``Settings.large_node_bytes``) are never loaded: their bytes are
first streamed through the same ``MultiFinder`` in chunks (byte mode), then a candidate is
confirmed with :func:`jsonreshape.stream.stream_walk`. Documented deviations of that
streaming path (ijson): duplicate keys are reported at every occurrence; ``-0`` reads as
``0`` and non-integer numbers are matched by their normalized text in plain/regex mode
(``1e400`` -> ``1E+400``, ``0.0000001`` -> ``1E-7``; number mode compares exact values and
is not affected); a lone surrogate escape reads as ``"?"``. A record that ``codec.loads``
cannot parse although it is valid JSON (nesting deeper than its recursion limit) is
walked the same streaming way.

Results and streaming
=====================
Hits are produced in source order (record order of ``source``, pre-order inside a
record). The first ``max_hits`` are stored -- and no more than fit in a quarter of
``Settings.memory_limit_bytes`` (estimated; the memory budget, "xotira chegarasi"); later
ones are only counted (``hit_count``; ``truncated``). The hits of a record are produced
incrementally (in batches of :data:`HIT_BATCH`) for streamed records and for parsed
records larger than :data:`INCREMENTAL_BYTES`, so with ``max_hits=0`` (``--count``) a
record with millions of matches costs no memory. ``records`` holds every matching record
in source order (a :class:`jsonreshape.recordset.RecordSet` when that module is
available, otherwise a plain list). ``on_hits(batch)`` is called from the searching
thread with the newly stored hits as soon as they are found -- the first batch
immediately, then at most every :data:`EMIT_INTERVAL` seconds and at the end. ``batch``
is a :class:`HitBatch` (a ``list``) that also carries the running totals; after
truncation empty batches keep the totals current. An exception raised by ``on_hits``
aborts the search. Progress is reported in bytes of record data read; ``cancel`` is
checked between records. When the scan has finished, ``doc.ensure_unchanged()`` makes
sure the file was not changed by another program meanwhile
(:class:`~jsonreshape.errors.FileChangedError`).

Other helpers: :func:`iter_hits` (every hit of the same pass, lazily, never stored -- for
exporting arbitrarily many hits), :func:`export_hits` (CSV/JSON, streamed, atomic),
:class:`SearchHistory` (``app_home()/search_history.json``: most recent first,
de-duplicated by search text, at most 200 entries).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import operator
import os
import threading
import time
from array import array
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Generator, Iterable, Iterator, Union

import orjson

from . import codec, core
from .appdirs import app_home
from .errors import JsonDecodeError, TooLargeError, UserInputError
from .fileio import atomic_write, long_path, make_dirs, open_shared, os_error, read_range
from .i18n import get_language, t
from .paths import PathPattern, Wild, canonical_index, format_path, parse_pattern
from .reader import split_member
from .settings import get_settings
from .stream import stream_walk
from .tasks import CancelToken, Progress, check, format_bytes
from .textmatch import APOSTROPHES, CANONICAL_APOSTROPHE, HUGE_TEXT, TextQuery, fold, translit_fold_ascii

log = logging.getLogger(__name__)

StrPath = Union[str, "os.PathLike[str]"]
Path_ = tuple[Union[str, int], ...]

__all__ = [
    "EXPORT_FORMATS",
    "SCOPES",
    "HitBatch",
    "SearchHistory",
    "SearchHit",
    "SearchOptions",
    "SearchResult",
    "export_hits",
    "iter_hits",
    "make_snippet",
    "reference_search",
    "search",
]

MiB = 1 << 20

SCOPES = ("both", "keys", "values")
EXPORT_FORMATS = ("csv", "json")
BLOCK_BYTES = 8 * MiB          # records read per block (Document.iter_blocks)
HUGE_CHUNK = 8 * MiB           # chunk size of the byte prefilter over a huge record
EMIT_INTERVAL = 0.1            # seconds between on_hits batches (the first one is immediate)
FOLLOW_SECONDS = 0.25          # a search started during indexing follows the index this long, then waits
CANCEL_EVERY = 256             # records (or stream events) between cancel checks
SNIPPET_MAX = 120              # strings up to this length are shown whole
SNIPPET_CONTEXT = 50           # characters shown before / after the first match
SNIPPET_REACH = 150            # a snippet ends at most this many characters after the first match starts
KEY_CACHE_MAX = 65536          # distinct keys whose result is cached per search
VALUE_CACHE_MAX = 16384        # distinct located value tokens whose result is cached per search
VALUE_CACHE_TOKEN = 256        # ... of at most this many bytes
LOCATE_NUMBER_BYTES = 1000     # located numbers are confirmed without parsing only in records up to this size
LOCATE_HEAD = 32               # candidates of a block located first, on their own (the first hits at once)
LOCATE = core.BACKEND == "rust"
"""Byte mode confirms candidates from their located tokens (``MultiFinder.locate_tokens``,
module docstring). The pure-Python core's tokenizer is slower than parsing a record with
orjson, so with that core every candidate is parsed instead (same result)."""
HIT_BATCH = 4096               # hits produced at a time by an incremental record walk
INCREMENTAL_BYTES = 1 << 20    # parsed records larger than this produce their hits incrementally
HIT_OVERHEAD = 320             # estimated bytes of a stored SearchHit besides its text, path and spans
HISTORY_MAX = 200
HISTORY_FILE = "search_history.json"
HISTORY_VERSION = 1
_HISTORY_MAX_BYTES = 8 * MiB    # a larger history file is treated as corrupt
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
CSV_COLUMNS = ("record", "key", "path", "where", "text")

_NUMBER_CHARS = frozenset("0123456789+-.eE")
_APOS_FOLD = str.maketrans({c: CANONICAL_APOSTROPHE for c in APOSTROPHES if c != CANONICAL_APOSTROPHE})


# ----------------------------------------------------------------------------------
# public data types
# ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class SearchOptions:
    """What to search for (spec §7).

    ``query``: the compiled :class:`~jsonreshape.textmatch.TextQuery`; ``scope``:
    ``"both"`` | ``"keys"`` | ``"values"``; ``path``: optional path filter (pattern
    syntax of :func:`jsonreshape.paths.parse_pattern`; blank = none). An invalid path raises
    :class:`~jsonreshape.errors.PathSyntaxError` here; an unknown scope ``ValueError``.
    """

    query: TextQuery
    scope: str = "both"
    path: str | None = None
    pattern: PathPattern | None = field(init=False, repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if not isinstance(self.query, TextQuery):
            raise TypeError(f"query must be a TextQuery, not {type(self.query).__name__}")
        if self.scope not in SCOPES:
            raise ValueError(f"unknown search scope {self.scope!r} (expected one of {', '.join(SCOPES)})")
        path = self.path
        if path is not None:
            if not isinstance(path, str):
                raise TypeError(f"path must be str or None, not {type(path).__name__}")
            if not path.strip():
                path = None
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "pattern", parse_pattern(path) if path is not None else None)

    @classmethod
    def build(cls, text: str, *, mode: str = "plain", case_sensitive: bool = False, whole_word: bool = False,
              translit: bool = False, scope: str = "both", path: str | None = None) -> SearchOptions:
        """Compile ``text`` into a :class:`TextQuery` and wrap it (convenience for GUI/CLI)."""
        query = TextQuery(text, mode=mode, case_sensitive=case_sensitive, whole_word=whole_word, translit=translit)
        return cls(query, scope=scope, path=path)

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible description (e.g. for :class:`SearchHistory`)."""
        q = self.query
        return {
            "text": q.text,
            "mode": q.mode,
            "case_sensitive": q.case_sensitive,
            "whole_word": q.whole_word,
            "translit": q.translit,
            "scope": self.scope,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SearchOptions:
        """Inverse of :meth:`to_dict` (missing options take their defaults)."""
        if not isinstance(data, dict):
            raise TypeError("search options must be a dict")
        return cls.build(
            str(data.get("text", "")),
            mode=str(data.get("mode", "plain")),
            case_sensitive=bool(data.get("case_sensitive", False)),
            whole_word=bool(data.get("whole_word", False)),
            translit=bool(data.get("translit", False)),
            scope=str(data.get("scope", "both")),
            path=data.get("path"),
        )


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One matched key or value.

    ``record``: 0-based record number; ``path``: path of the key/value inside the record
    value (``()`` = the record itself; a key hit's path ends with the key); ``where``:
    ``"key"`` | ``"value"``; ``text``: the matched text or a snippet of it (module
    docstring); ``spans``: ``(start, end)`` match spans relative to ``text``; ``key``: the
    record's member key (keyed documents), else ``None``.
    """

    record: int
    path: Path_
    where: str
    text: str
    spans: tuple[tuple[int, int], ...]
    key: str | None = None

    @property
    def path_text(self) -> str:
        """Dotted path (``"$"`` for the record itself)."""
        return format_path(self.path) if self.path else "$"

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible form (the objects written by :func:`export_hits` as JSON)."""
        return {
            "record": self.record,
            "key": self.key,
            "path": self.path_text,
            "where": self.where,
            "text": self.text,
            "spans": [list(s) for s in self.spans],
        }


class HitBatch(list):
    """The newly stored hits passed to ``on_hits`` (a ``list`` of :class:`SearchHit`),
    plus the running totals of the search at the moment of the call."""

    __slots__ = ("hit_count", "record_count", "truncated", "bytes_scanned")

    def __init__(self, hits: Iterable[SearchHit] = (), *, hit_count: int = 0, record_count: int = 0,
                 truncated: bool = False, bytes_scanned: int = 0) -> None:
        super().__init__(hits)
        self.hit_count = hit_count
        self.record_count = record_count
        self.truncated = truncated
        self.bytes_scanned = bytes_scanned


@dataclass
class SearchResult:
    """Outcome of :func:`search`.

    ``hits``: the stored hits (at most ``max_hits``) in source order; ``hit_count``: all
    hits; ``record_count``: matching records; ``records``: those records in source order
    (RecordSet, or a plain list when the recordset module is unavailable);
    ``truncated``: some hits were counted but not stored; ``mode_used``: ``"bytes"`` |
    ``"parse"``; ``bytes_scanned``: record bytes read; ``elapsed``: seconds;
    ``records_scanned`` / ``records_parsed``: records visited / parsed and confirmed.
    """

    hits: list[SearchHit]
    hit_count: int
    record_count: int
    records: Any
    truncated: bool
    mode_used: str
    bytes_scanned: int
    elapsed: float
    records_scanned: int = 0
    records_parsed: int = 0

    @property
    def mb_per_s(self) -> float:
        """Scan speed in MB/s (decimal megabytes)."""
        return self.bytes_scanned / 1e6 / self.elapsed if self.elapsed > 0 else 0.0

    def close(self) -> None:
        """Release the record set (its temporary file, if any); the hits stay usable."""
        close = getattr(self.records, "close", None)
        if close is not None:
            close()

    def summary(self) -> str:
        """One localized line: hits, records, speed, mode (and the truncation note); counts
        with grouped digits, the English singular for one."""
        text = t("search.summary", hits=_count_text(self.hit_count), records=_count_text(self.record_count),
                 size=format_bytes(self.bytes_scanned), seconds=f"{self.elapsed:.2f}",
                 rate=f"{self.mb_per_s:.0f}", mode=t(f"search.mode.{self.mode_used}"))
        if self.truncated:
            key = "search.summary.truncated.one" if len(self.hits) == 1 else "search.summary.truncated"
            text += " " + t(key, shown=_count_text(len(self.hits)), total=_count_text(self.hit_count))
        return text


def _count_text(n: int) -> str:
    """A count with grouped digits: ``12 345`` (Uzbek), ``12,345`` (English)."""
    text = f"{int(n):,}"
    return text.replace(",", " ") if get_language() == "uz" else text


# ----------------------------------------------------------------------------------
# snippets
# ----------------------------------------------------------------------------------
def make_snippet(text: str, spans: Iterable[tuple[int, int]]) -> tuple[str, tuple[tuple[int, int], ...]]:
    """``(snippet, spans relative to the snippet)`` for a matched string (module
    docstring): the whole text up to :data:`SNIPPET_MAX` characters, else a window around
    the first match with ``"…"`` at cut ends."""
    if type(spans) is not tuple:
        spans = tuple(spans)
    n = len(text)
    if n <= SNIPPET_MAX or not spans:
        return text, spans  # type: ignore[return-value]
    a, b = spans[0]
    start = max(0, a - SNIPPET_CONTEXT)
    end = min(n, max(a, b) + SNIPPET_CONTEXT, a + SNIPPET_REACH)
    head = ELLIPSIS if start > 0 else ""
    tail = ELLIPSIS if end < n else ""
    shift = len(head) - start
    rel = []
    for x, y in spans:
        if (x < end and y > start) or (x == y and start <= x <= end):
            rel.append((max(x, start) + shift, min(y, end) + shift))
    return head + text[start:end] + tail, tuple(rel)


# ----------------------------------------------------------------------------------
# path filter (an NFA over pattern positions, like PathPattern._resolve_walk)
# ----------------------------------------------------------------------------------
_DEAD = frozenset({-1})   # no node at or below can pass the filter
"""Filter state of a subtree that cannot match (distinct from every real state)."""
_MISS = object()
_LITERAL_TEXT = {ord("t"): "true", ord("f"): "false", ord("n"): "null"}


class _NeedParse(Exception):
    """A located record whose hits the locator cannot prove: confirm it by parsing."""


class _PathFilter:
    """``pattern.matches_prefix_of(path)`` evaluated incrementally along a walk.

    A state is ``None`` (a prefix of the path matched: the node and everything below it
    pass), :data:`_DEAD` (nothing at or below can match) or a frozenset of pattern
    positions reached so far (the node itself does not pass).
    """

    __slots__ = ("segs", "n", "initial", "_closures", "_steps")

    def __init__(self, pattern: PathPattern) -> None:
        self.segs = pattern.segments
        self.n = len(self.segs)
        self._closures: dict[frozenset[int], frozenset[int]] = {}
        self._steps: dict[tuple[frozenset[int], Any], frozenset[int] | None] = {}
        start = self._closure(frozenset({0}))
        self.initial: frozenset[int] | None = None if self.n in start else start

    def _closure(self, states: frozenset[int]) -> frozenset[int]:
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

    def step(self, states: frozenset[int], seg: str | int) -> frozenset[int] | None:
        """State of the child ``seg`` of a node in state ``states`` (not ``None``/dead)."""
        key = (states, seg)
        res = self._steps.get(key, _MISS)
        if res is not _MISS:
            return res  # type: ignore[return-value]
        nxt: set[int] = set()
        segs = self.segs
        for i in states:
            if i >= self.n:
                continue
            p = segs[i]
            if p is Wild.DESCENDANTS:
                nxt.add(i)
            elif p is Wild.ANY_INDEX:
                if isinstance(seg, int):
                    nxt.add(i + 1)
            elif p is Wild.ANY_KEY:
                if isinstance(seg, str):
                    nxt.add(i + 1)
            elif isinstance(p, int):
                if isinstance(seg, int) and seg == p:
                    nxt.add(i + 1)
            elif isinstance(seg, int):
                if canonical_index(p) == seg:
                    nxt.add(i + 1)
            elif seg == p:
                nxt.add(i + 1)
        if not nxt:
            res = _DEAD
        else:
            closed = self._closure(frozenset(nxt))
            res = None if self.n in closed else closed
        if len(self._steps) >= KEY_CACHE_MAX:
            self._steps.clear()
        self._steps[key] = res
        return res

    def state_for(self, path: Path_) -> frozenset[int] | None:
        """State of the node at ``path`` (from the record root)."""
        st = self.initial
        for seg in path:
            if st is None or st is _DEAD:
                return st
            st = self.step(st, seg)
        return st


# ----------------------------------------------------------------------------------
# the confirm function
# ----------------------------------------------------------------------------------
RawHit = tuple[Path_, str, str, tuple[tuple[int, int], ...]]   # (path, where, full text, spans)


class _Matcher:
    """The confirm step shared by :func:`search` and :func:`reference_search`."""

    def __init__(self, opts: SearchOptions) -> None:
        q = opts.query
        self.query = q

        def snippet_spans(s: str, _find: Callable[..., list[tuple[int, int]]] = q.find_spans) -> list[tuple[int, int]]:
            # a long string: only the spans its snippet can show (module docstring)
            return _find(s) if len(s) <= SNIPPET_MAX else _find(s, SNIPPET_REACH)

        self.find: Callable[[str], list[tuple[int, int]]] = snippet_spans
        number_mode = q.mode == "number"
        self.number_mode = number_mode
        self.keys_on = opts.scope != "values" and not number_mode
        self.values_on = opts.scope != "keys"
        self.strings_on = self.values_on and not number_mode
        self.filter = _PathFilter(opts.pattern) if opts.pattern is not None else None
        self.root_state = self.filter.initial if self.filter is not None else None
        # Cheap necessary condition of a plain match, tested before find_spans: the folded
        # needle must occur in the folded text (TextQuery._plain_spans starts with exactly
        # this test), so its longest apostrophe-free part occurs in s.casefold() (in s when
        # case-sensitive) -- apostrophe folding only turns variants into "'".
        self.cf_needle: str | None = None            # inline test: cf_needle in s.casefold()
        self.cs_needle: str | None = None            # inline test: cs_needle in s
        self.pre: Callable[[str], bool] | None = None   # the same test as a function
        numbers_possible = True
        if q.mode == "plain" and not q.translit:
            needle = fold(q.text, casefold=not q.case_sensitive)[0]
            parts = [p for p in needle.split(CANONICAL_APOSTROPHE) if p]
            if parts:
                part = max(parts, key=len)
                if q.case_sensitive:
                    self.cs_needle = part
                    self.pre = lambda s, _n=part: _n in s
                else:
                    self.cf_needle = part
                    self.pre = lambda s, _n=part: _n in s.casefold()
            elif needle:  # only apostrophes
                if q.case_sensitive:
                    self.pre = lambda s, _n=needle: _n in s.translate(_APOS_FOLD)
                else:
                    self.pre = lambda s, _n=needle: _n in s.casefold().translate(_APOS_FOLD)
            # the folded text of a number only holds digits, "+-.e" (and "E" when
            # case-sensitive): a needle with another character never matches a number
            numbers_possible = set(needle) <= _NUMBER_CHARS
        elif q.mode == "plain":
            # Latin <-> Cyrillic: the needle is searched as several alternatives (its first
            # and last characters folded in every context), but the folding of the inner
            # characters is the same in all of them, so every match contains that "core".
            # An ASCII string has no Cyrillic letters, so its transliterated folding is
            # textmatch.translit_fold_ascii(s) (case and apostrophe folding plus the
            # optional apostrophe between s and h): the core's longest apostrophe-free
            # part must occur in it; other strings always go to find_spans.
            folded, imap = fold(q.text, casefold=not q.case_sensitive, translit=True)
            last = len(q.text) - 1
            core = "".join(ch for ch, src in zip(folded, imap) if 1 <= src < last)
            parts = [p for p in core.split(CANONICAL_APOSTROPHE) if p]
            if parts:
                part = max(parts, key=len)
                casefold = not q.case_sensitive
                self.pre = lambda s, _n=part, _cf=casefold: not s.isascii() or _n in translit_fold_ascii(s, _cf)
            numbers_possible = set(core) <= _NUMBER_CHARS
        self.numbers_on = self.values_on and (number_mode or numbers_possible)
        self.match_number = q.match_number
        self.key_cache: dict[str, tuple[tuple[int, int], ...]] = {}
        # the locator path (byte mode): raw token -> (text, spans) | None; path -> passes the filter
        self.value_cache: dict[bytes, tuple[str, tuple[tuple[int, int], ...]] | None] = {}
        self.pass_cache: dict[Path_, bool] = {}
        if self.values_on and not number_mode:
            self.lit = {name: tuple(self.find(name)) for name in ("null", "true", "false")}
        else:
            self.lit = {"null": (), "true": (), "false": ()}
        # can a number / true / false / null value ever be a hit?
        self.other_on = self.values_on and (self.numbers_on or any(self.lit.values()))

    # -- single items ----------------------------------------------------------------
    def key_spans(self, k: str) -> tuple[tuple[int, int], ...]:
        """Match spans of an object key (cached per search)."""
        cached = self.key_cache.get(k)
        if cached is not None:
            return cached
        pre = self.pre
        res = tuple(self.find(k)) if (pre is None or pre(k)) else ()
        if len(self.key_cache) >= KEY_CACHE_MAX:
            self.key_cache.clear()
        self.key_cache[k] = res
        return res

    def scalar(self, v: Any) -> tuple[str, tuple[tuple[int, int], ...]] | None:
        """``(text, spans)`` of a matching scalar value, else ``None``."""
        if isinstance(v, str):
            if not self.strings_on:
                return None
            pre = self.pre
            # a huge string skips the prefilter (it would build a folded copy of it)
            if pre is not None and len(v) <= HUGE_TEXT and not pre(v):
                return None
            spans = self.find(v)
            return (v, tuple(spans)) if spans else None
        if v is None or v is True or v is False:
            text = "null" if v is None else "true" if v else "false"
            spans = self.lit[text]
            return (text, spans) if spans else None
        if isinstance(v, (int, float, Decimal)):
            if not self.numbers_on:
                return None
            text = codec.number_text(v)
            if self.number_mode:  # find_spans would give exactly this
                return (text, ((0, len(text)),)) if self.match_number(text) else None
            pre = self.pre
            if pre is not None and not pre(text):
                return None
            spans = self.find(text)
            return (text, tuple(spans)) if spans else None
        return None

    # -- located tokens (byte mode) --------------------------------------------------
    def _cache_value(self, tok: bytes, r: tuple[str, tuple[tuple[int, int], ...]] | None) -> None:
        if len(tok) <= VALUE_CACHE_TOKEN:
            cache = self.value_cache
            if len(cache) >= VALUE_CACHE_MAX:
                cache.clear()
            cache[tok] = r

    def string_hit(self, tok: bytes) -> tuple[str, tuple[tuple[int, int], ...]] | None:
        """:meth:`scalar` of the string token ``tok`` (raw JSON, quotes included; cached
        per raw token). :class:`_NeedParse` when no decoder accepts the token alone."""
        r = self.value_cache.get(tok, _MISS)
        if r is not _MISS:
            return r  # type: ignore[return-value]
        try:
            v = orjson.loads(tok)
        except orjson.JSONDecodeError:
            try:
                v = codec.loads(tok)  # what orjson refuses (lone surrogate escapes)
            except JsonDecodeError:
                raise _NeedParse from None
        if type(v) is not str:  # pragma: no cover - the core located a string token
            raise _NeedParse
        r = self.scalar(v)
        self._cache_value(tok, r)
        return r

    def number_hit(self, tok: bytes) -> tuple[str, tuple[tuple[int, int], ...]] | None:
        """:meth:`scalar` of a number whose exact JSON text is ``tok`` (``number_text``
        of the value ``codec.loads`` gives is that text; cached per token)."""
        r = self.value_cache.get(tok, _MISS)
        if r is not _MISS:
            return r  # type: ignore[return-value]
        text = tok.decode("ascii")
        if not self.numbers_on:
            r = None
        elif self.number_mode:  # find_spans would give exactly this
            r = (text, ((0, len(text)),)) if self.match_number(text) else None
        else:
            pre = self.pre
            if pre is not None and not pre(text):
                r = None
            else:
                spans = self.find(text)
                r = (text, tuple(spans)) if spans else None
        self._cache_value(tok, r)
        return r

    def path_passes(self, path: Path_) -> bool:
        """``pattern.matches_prefix_of(path)`` of the path filter (cached per path)."""
        r = self.pass_cache.get(path)
        if r is None:
            r = self.filter.state_for(path) is None  # type: ignore[union-attr]
            if len(self.pass_cache) >= KEY_CACHE_MAX:
                self.pass_cache.clear()
            self.pass_cache[path] = r
        return r

    def locate_records(self, data: bytes, toks: list[tuple], ti: int, cands: Any, sp: array, indices: Any,
                       member: bool, stop: int, limit: int, batch: _RecordBatch,
                       count_only: bool = False) -> tuple[int, int, int]:
        """Confirm records from their located tokens (byte mode, module docstring).

        ``toks`` is ``MultiFinder.locate_tokens(data, spans of cands, member)``: its span
        index ``j`` stands for the candidate ``k = cands[j]`` of the block, whose span is
        ``data[sp[2k]:sp[2k+1]]`` and whose record number is ``indices[k]``. The records
        from token ``ti`` on are confirmed in order -- at most ``limit`` of them, none at or
        after candidate ``stop`` -- and those with hits are appended to ``batch``; their
        raw hits are exactly :meth:`match` of the parsed record (``count_only``: only the
        record numbers and the number of hits, see :class:`_RecordBatch`). Returns ``(next
        token index, records done, k)``: ``k >= 0`` is the last record done, which must be
        confirmed by parsing instead (its tokens are skipped): duplicate keys (kind 5), a
        number that could be a hit in a record too long to rule out a nesting depth the
        in-memory decoder refuses, a token no decoder accepts alone."""
        n = len(toks)
        filt = self.filter
        passes = self.path_passes
        strings_on = self.strings_on
        keys_on = self.keys_on
        values_on = self.values_on
        numbers_on = self.numbers_on
        kcache = self.key_cache
        key_spans = self.key_spans
        vcache = self.value_cache
        string_hit = self.string_hit
        lit = self.lit
        add_index = batch.indices.append
        add_key = batch.keys.append
        add_raw = batch.raws.append
        hits = 0
        done = 0
        k = -1
        try:
            while ti < n and done < limit:
                t = toks[ti]
                j = t[0]
                k = cands[j]
                if k >= stop:
                    break
                done += 1
                out: list[RawHit] = []
                key: str | None = None
                while True:
                    kind = t[1]
                    if kind == 0:  # string value
                        if strings_on and (filt is None or passes(t[4])):
                            tok = data[t[2]:t[3]]
                            r = vcache.get(tok, _MISS)
                            if r is _MISS:
                                r = string_hit(tok)
                            if r is not None:
                                out.append(True if count_only else (t[4], "value", r[0], r[1]))  # type: ignore
                    elif kind == 1:  # object key; the path ends with it
                        if keys_on:
                            path = t[4]
                            if filt is None or passes(path):
                                name = path[-1]
                                ks = kcache.get(name)
                                if ks is None:
                                    ks = key_spans(name)
                                if ks:
                                    out.append(True if count_only else (path, "key", name, ks))  # type: ignore
                    elif kind == 2:  # number
                        if numbers_on and (filt is None or passes(t[4])):
                            if sp[2 * k + 1] - sp[2 * k] > LOCATE_NUMBER_BYTES:
                                # a record nested deeper than codec.loads accepts is streamed
                                # by the confirm step, which reads numbers differently
                                raise _NeedParse
                            r = self.number_hit(data[t[2]:t[3]])
                            if r is not None:
                                out.append((t[4], "value", r[0], r[1]))
                    elif kind == 3:  # true / false / null
                        if values_on:
                            text = _LITERAL_TEXT[data[t[2]]]
                            spans = lit[text]
                            if spans and (filt is None or passes(t[4])):
                                out.append((t[4], "value", text, spans))
                    elif kind == 4:  # the record's own key (member records)
                        if keys_on and self.root_state is None:
                            key = split_member(data[sp[2 * k]:sp[2 * k + 1]])[0]
                            ks = key_spans(key)
                            if ks:
                                out.append(((), "key", key, ks))
                    else:  # 5: duplicate keys in the record (dict semantics)
                        raise _NeedParse
                    ti += 1
                    if ti == n:
                        break
                    t = toks[ti]
                    if t[0] != j:
                        break
                if out:
                    add_index(indices[k])
                    hits += len(out)
                    if not count_only:
                        if member and key is None:
                            key = split_member(data[sp[2 * k]:sp[2 * k + 1]])[0]
                        add_key(key)
                        add_raw(out)
            k = -1
        except _NeedParse:
            j = toks[ti][0]
            while ti < n and toks[ti][0] == j:
                ti += 1
        batch.hits += hits
        return ti, done, k

    # -- parsed records --------------------------------------------------------------
    def match(self, value: Any, key: str | None = None) -> list[RawHit]:
        """Raw hits ``(path, where, text, spans)`` of one parsed record, in pre-order.
        ``key``: the member key of a member record (checked first, at path ``()``)."""
        out: list[RawHit] = []
        st0 = self.root_state
        if key is not None and self.keys_on and st0 is None:
            spans = self.key_spans(key)
            if spans:
                out.append(((), "key", key, spans))
        tv = type(value)
        if tv is dict or tv is list:
            if value:
                if st0 is None:
                    self._walk_all(value, out)
                else:
                    for batch in self._walk_filtered(value, st0):
                        out.extend(batch)
        elif st0 is None and self.values_on:
            r = self.scalar(value)
            if r is not None:
                out.append(((), "value", r[0], r[1]))
        return out

    def match_iter(self, value: Any, key: str | None = None) -> Iterator[list[RawHit]]:
        """:meth:`match` produced incrementally: lists of at most about
        :data:`HIT_BATCH` raw hits (for big records: nothing is kept between batches)."""
        st0 = self.root_state
        tv = type(value)
        if not ((tv is dict or tv is list) and value):
            first = self.match(value, key)
            if first:
                yield first
            return
        out: list[RawHit] = []
        if key is not None and self.keys_on and st0 is None:
            spans = self.key_spans(key)
            if spans:
                out.append(((), "key", key, spans))
        if st0 is None:
            stack: list[Any] | None = None
            while True:
                stack = self._walk_all(value, out, stack, HIT_BATCH)
                if out:
                    yield out
                    out = []
                if stack is None:
                    return
        if out:
            yield out
        yield from self._walk_filtered(value, st0)

    def _walk_all(self, root: dict | list, out: list[RawHit], stack: list[Any] | None = None,
                  limit: int | None = None) -> list[Any] | None:
        """Iterative pre-order walk (no recursion limit) where every node passes the path
        filter; the per-item tests are inlined (this loop is the search's hot spot).

        With ``limit``, the walk stops as soon as ``out`` holds ``limit`` hits and returns
        its stack; calling it again with that stack continues where it stopped (the
        stack holds live iterators). Returns ``None`` when the walk is complete."""
        keys_on = self.keys_on
        strings_on = self.strings_on
        other_on = self.other_on
        cf_needle = self.cf_needle
        cs_needle = self.cs_needle
        pre = None if (cf_needle is not None or cs_needle is not None) else self.pre
        find = self.find
        kcache = self.key_cache
        key_spans = self.key_spans
        scalar = self.scalar
        append = out.append
        cap = limit if limit is not None else -1
        if stack is None:
            stack = []
            if type(root) is dict:
                stack.append((iter(root.items()), (), True))
            else:
                stack.append((enumerate(root), (), False))
        while stack:
            it, path, is_dict = stack[-1]
            if is_dict:
                for k, v in it:
                    if keys_on:
                        ks = kcache.get(k)
                        if ks is None:
                            ks = key_spans(k)
                        if ks:
                            append((path + (k,), "key", k, ks))
                            if len(out) == cap:
                                # resume with this member's value (its key is done)
                                stack.append((enumerate((v,)), path + (k,), None))
                                return stack
                    tv = type(v)
                    if tv is str:
                        if strings_on:
                            if cf_needle is not None:
                                if cf_needle not in v.casefold():
                                    continue
                            elif cs_needle is not None:
                                if cs_needle not in v:
                                    continue
                            elif pre is not None and not pre(v):
                                continue
                            sp = find(v)
                            if sp:
                                append((path + (k,), "value", v, tuple(sp)))
                                if len(out) == cap:
                                    return stack
                    elif tv is dict:
                        if v:
                            stack.append((iter(v.items()), path + (k,), True))
                            break
                    elif tv is list:
                        if v:
                            stack.append((enumerate(v), path + (k,), False))
                            break
                    elif other_on:
                        r = scalar(v)
                        if r is not None:
                            append((path + (k,), "value", r[0], r[1]))
                            if len(out) == cap:
                                return stack
                else:
                    stack.pop()
            else:
                # is_dict False: array elements (path + (i,)); None: the single pending
                # value of a member whose key hit ended the previous call (at ``path``)
                member = is_dict is None
                for i, v in it:
                    vpath = path if member else path + (i,)
                    tv = type(v)
                    if tv is str:
                        if strings_on:
                            if cf_needle is not None:
                                if cf_needle not in v.casefold():
                                    continue
                            elif cs_needle is not None:
                                if cs_needle not in v:
                                    continue
                            elif pre is not None and not pre(v):
                                continue
                            sp = find(v)
                            if sp:
                                append((vpath, "value", v, tuple(sp)))
                                if len(out) == cap:
                                    return stack
                    elif tv is dict:
                        if v:
                            stack.append((iter(v.items()), vpath, True))
                            break
                    elif tv is list:
                        if v:
                            stack.append((enumerate(v), vpath, False))
                            break
                    elif other_on:
                        r = scalar(v)
                        if r is not None:
                            append((vpath, "value", r[0], r[1]))
                            if len(out) == cap:
                                return stack
                else:
                    stack.pop()
        return None

    def _walk_filtered(self, root: dict | list, st0: frozenset[int]) -> Iterator[list[RawHit]]:
        """Like :meth:`_walk_all` with the path filter's NFA state carried along: a
        subtree that cannot match is skipped, one that matched is searched whole.
        Produces the hits in lists of about :data:`HIT_BATCH`."""
        keys_on = self.keys_on
        strings_on = self.strings_on
        other_on = self.other_on
        pre = self.pre
        find = self.find
        key_spans = self.key_spans
        scalar = self.scalar
        step = self.filter.step  # type: ignore[union-attr]
        walk_all = self._walk_all
        dead = _DEAD
        out: list[RawHit] = []
        append = out.append
        stack: list[tuple[Iterator[Any], Path_, frozenset[int], bool]] = []
        if type(root) is dict:
            stack.append((iter(root.items()), (), st0, True))
        else:
            stack.append((enumerate(root), (), st0, False))
        while stack:
            it, path, st, is_dict = stack[-1]
            for seg, v in it:
                cst = step(st, seg)
                if cst is dead:
                    continue
                if cst is None:  # this node matched: it and everything below it pass
                    child = path + (seg,)
                    if is_dict and keys_on:
                        ks = key_spans(seg)
                        if ks:
                            append((child, "key", seg, ks))
                    tv = type(v)
                    if tv is dict or tv is list:
                        if v:
                            sub: list[RawHit] = []
                            sub_stack: list[Any] | None = None
                            while True:
                                sub_stack = walk_all(v, sub, sub_stack, HIT_BATCH)
                                for p, where, text, sp in sub:
                                    append((child + p, where, text, sp))
                                sub.clear()
                                if len(out) >= HIT_BATCH:
                                    yield out
                                    out = []
                                    append = out.append
                                if sub_stack is None:
                                    break
                    elif tv is str:
                        if strings_on and (pre is None or len(v) > HUGE_TEXT or pre(v)):
                            sp = find(v)
                            if sp:
                                append((child, "value", v, tuple(sp)))
                    elif other_on:
                        r = scalar(v)
                        if r is not None:
                            append((child, "value", r[0], r[1]))
                    if len(out) >= HIT_BATCH:
                        yield out
                        out = []
                        append = out.append
                    continue
                tv = type(v)
                if (tv is dict or tv is list) and v:
                    stack.append((iter(v.items()) if tv is dict else enumerate(v), path + (seg,), cst, tv is dict))
                    break
            else:
                stack.pop()
        if out:
            yield out

    # -- streamed (huge) records -----------------------------------------------------
    def match_events(self, events: Iterable[tuple[Path_, str, Any]], key: str | None = None,
                     cancel: CancelToken | None = None) -> Iterator[list[RawHit]]:
        """Raw hits from :func:`jsonreshape.stream.stream_walk` events (huge records), with
        the same per-item tests as :meth:`match`, produced in lists of at most
        :data:`HIT_BATCH` hits (nothing else is kept)."""
        out: list[RawHit] = []
        st0 = self.root_state
        if key is not None and self.keys_on and st0 is None:
            spans = self.key_spans(key)
            if spans:
                out.append(((), "key", key, spans))
        if st0 is _DEAD:
            if out:
                yield out
            return
        filt = self.filter
        keys_on = self.keys_on
        values_on = self.values_on
        n = 0
        for path, kind, val in events:
            n += 1
            if n % CANCEL_EVERY == 0:
                check(cancel)
            if kind == "key":
                if not keys_on:
                    continue
            elif not values_on:
                continue
            if filt is not None and filt.state_for(path) is not None:
                continue
            if kind == "key":
                spans = self.key_spans(val)
                if spans:
                    out.append((path, "key", val, spans))
            else:
                r = self.scalar(val)
                if r is not None:
                    out.append((path, "value", r[0], r[1]))
            if len(out) >= HIT_BATCH:
                yield out
                out = []
        if out:
            yield out


def _make_hit(record: int, key: str | None, raw: RawHit) -> SearchHit:
    path, where, text, spans = raw
    snippet, rel = make_snippet(text, spans)
    return SearchHit(int(record), path, where, snippet, rel, key)


# ----------------------------------------------------------------------------------
# record access helpers
# ----------------------------------------------------------------------------------
def _loads_ignoring_numbers(buf: bytes) -> Any:
    """``codec.loads`` for searches where no number can be a hit: plain ``orjson.loads``
    (keys, strings, literals and structure are identical; only the text of non-plain
    numbers such as ``1.10`` may differ, and it is never looked at), ``codec.loads`` for
    anything orjson refuses (``1e400``, lone surrogate escapes, very deep nesting)."""
    try:
        return orjson.loads(buf)
    except orjson.JSONDecodeError:
        return codec.loads(buf)


def _parse_record(buf: bytes, member: bool, loads: Callable[[bytes], Any] = codec.loads) -> tuple[Any, str | None]:
    """``(value, member key)`` of raw record bytes (``JsonDecodeError`` when the record
    cannot be parsed in memory, e.g. nesting deeper than the decoder's limit)."""
    if member:
        key, vo = split_member(buf)
        return loads(buf[vo:]), key
    return loads(buf), None


def _stream_hits(doc: Any, matcher: _Matcher, index: int, span: tuple[int, int], member: bool,
                 cancel: CancelToken | None) -> tuple[_HitStream, str | None]:
    """Confirm a record without loading it (stream_walk); the hits are produced lazily."""
    key = doc.record_key(index) if member else None
    hits = _HitStream(matcher.match_events(stream_walk(doc, span, member=member), key, cancel))
    return hits, key


class _HitStream:
    """The hits of one record produced incrementally: an iterator of lists of raw hits
    (possibly none at all). :func:`_scan` yields it instead of a plain (non-empty) list
    for streamed and big parsed records."""

    __slots__ = ("batches",)

    def __init__(self, batches: Iterator[list[RawHit]]) -> None:
        self.batches = batches

    def __iter__(self) -> Iterator[list[RawHit]]:
        return self.batches


def _raw_batches(raw: list[RawHit] | _HitStream) -> Iterator[list[RawHit]] | tuple[list[RawHit], ...]:
    """The hit lists of one record of :func:`_scan` (a list: one batch)."""
    return (raw,) if type(raw) is list else raw.batches


class _RecordBatch:
    """Consecutive matching records of a byte-mode block, yielded by :func:`_scan` as one
    item: parallel ``indices`` / ``keys`` (member keys) / ``raws`` (non-empty hit lists)
    and ``hits``, the total number of hits (so a consumer that only counts handles the
    batch in a few C-level operations). Once the consumer only counts
    (``_ScanStats.count_only``, set by :func:`search` when the stored hits are complete)
    the batches hold just ``indices`` and ``hits`` (``keys`` and ``raws`` stay empty)."""

    __slots__ = ("indices", "keys", "raws", "hits")

    def __init__(self) -> None:
        self.indices = array("Q")
        self.keys: list[str | None] = []
        self.raws: list[list[RawHit]] = []
        self.hits = 0

    def __len__(self) -> int:
        return len(self.indices)


def _huge_contains(doc: Any, span: tuple[int, int], finder: Any, maxlen: int, cancel: CancelToken | None) -> bool:
    """Whether a needle occurs inside ``span`` (read in chunks that overlap by
    ``maxlen - 1`` bytes, so no occurrence is missed)."""
    start, end = span
    overlap = max(0, maxlen - 1)
    with open_shared(doc.data_path, "rb") as f:
        pos = start
        while pos < end:
            check(cancel)
            stop = min(end, pos + max(HUGE_CHUNK, 2 * overlap + 1))
            chunk = read_range(f, pos, stop)
            if finder.find_first(chunk) >= 0:
                return True
            if stop >= end:
                break
            pos = stop - overlap
    return False


def _iter_indices(source: Any) -> Iterator[int]:
    """Record indices of a source (same conventions as ``Document.iter_blocks``)."""
    if isinstance(source, (list, tuple, array, range)):
        yield from source
    elif hasattr(source, "iter_chunks"):
        for chunk in source.iter_chunks():
            yield from chunk
    elif hasattr(source, "__len__") and hasattr(source, "__getitem__") and not isinstance(source, (dict, str)):
        for k in range(len(source)):
            yield source[k]
    else:
        yield from source


def _source_total(doc: Any, source: Any) -> int | None:
    """Bytes the scan will read (exact for ``None`` and ranges; estimated from the average
    record size for other sized sources; ``None`` when unknown)."""
    if source is None:
        return max(0, int(doc.data_size) - int(doc.info.data_start))
    if not doc.index_complete:
        return None
    count = doc.record_count
    try:
        if isinstance(source, range) and source.step == 1:
            if len(source) == 0:
                return 0
            if source.start < 0 or source.stop > count:
                return None
            return doc.record_span(source.stop - 1)[1] - doc.record_span(source.start)[0]
        n = len(source)
    except (TypeError, IndexError):
        return None
    if count == 0 or n == 0:
        return 0
    first = doc.record_span(0)[0]
    last = doc.record_span(count - 1)[1]
    return int((last - first) * n / count)


# ----------------------------------------------------------------------------------
# result collection
# ----------------------------------------------------------------------------------
try:  # written in parallel with this module; a plain list is the fallback
    from .recordset import RecordSetBuilder as _RecordSetBuilder
except Exception as _exc:  # pragma: no cover - recordset module unavailable or broken
    log.warning("jsonreshape.recordset unavailable (%s); search results use plain lists", _exc)
    _RecordSetBuilder = None  # type: ignore[assignment,misc]


class _RecordSink:
    """Collects matching record numbers: a :class:`jsonreshape.recordset.RecordSet`
    (``kind="search"``) when that module is available, else a plain list. Indices are
    buffered and handed over per block (``RecordSetBuilder.extend``)."""

    def __init__(self, description: str) -> None:
        self._builder: Any = _RecordSetBuilder("search", description) if _RecordSetBuilder is not None else None
        self._items: list[int] = []
        self._buf = array("Q")

    def append(self, i: int) -> None:
        self._buf.append(i)

    def extend(self, indices: array) -> None:
        self._buf.extend(indices)

    def flush(self) -> None:
        if not self._buf:
            return
        if self._builder is not None:
            self._builder.extend(self._buf)
        else:
            self._items.extend(self._buf)
        self._buf = array("Q")

    def finish(self) -> Any:
        self.flush()
        if self._builder is not None:
            return self._builder.finish()
        return self._items

    def abort(self) -> None:
        """Discard a partial set (its temporary file is removed)."""
        self._buf = array("Q")
        if self._builder is not None:
            try:
                self._builder.abort()
            except Exception:  # pragma: no cover - best effort cleanup
                log.warning("could not discard the partial search record set", exc_info=True)


def _hit_cost(hit: SearchHit) -> int:
    """Estimated bytes of a stored hit (the memory budget of :func:`search`)."""
    return HIT_OVERHEAD + 2 * len(hit.text) + 48 * len(hit.spans) + 16 * len(hit.path) + (
        len(hit.key) if hit.key is not None else 0)


class _Collector:
    """Stores the first ``max_hits`` hits (and no more than ``budget`` estimated bytes),
    counts all of them, feeds ``on_hits``."""

    def __init__(self, max_hits: int | None, on_hits: Callable[[HitBatch], Any] | None, sink: _RecordSink,
                 budget: int | None = None) -> None:
        self.max_hits = max_hits
        self.on_hits = on_hits
        self.sink = sink
        self.budget = budget
        self.stored_bytes = 0
        self.hits: list[SearchHit] = []
        self.hit_count = 0
        self.record_count = 0
        self.truncated = False
        self.bytes_scanned = 0
        self._pending: list[SearchHit] = []
        self._last_emit: float | None = None
        self._emitted_counts = (0, 0)

    def add(self, record: int, key: str | None, raw: list[RawHit] | _HitStream) -> None:
        """Hits of one record, in order: a non-empty list, or a :class:`_HitStream` whose
        batches are consumed here (the record counts when it yields at least one hit)."""
        counted = False
        for batch in _raw_batches(raw):
            if not batch:
                continue
            if not counted:
                counted = True
                self.record_count += 1
                self.sink.append(int(record))
            self._add_batch(record, key, batch)
            if self.on_hits is not None:
                self.maybe_emit()

    def add_records(self, batch: _RecordBatch) -> None:
        """The records of a :class:`_RecordBatch`, in order (same result as :meth:`add`
        for each of them); once the stored hits are complete they are only counted (a
        count-only batch comes only then)."""
        raws = batch.raws
        n = len(batch.indices)
        k = 0
        while k < n and not self.truncated:
            self.record_count += 1
            self.sink.append(batch.indices[k])
            self._add_batch(batch.indices[k], batch.keys[k], raws[k])
            k += 1
        if k < n:
            if k:
                self.hit_count += sum(map(len, raws[k:]))
                self.sink.extend(batch.indices[k:])
            else:
                self.hit_count += batch.hits
                self.sink.extend(batch.indices)
            self.record_count += n - k
        if k and self.on_hits is not None:
            self.maybe_emit()

    def _add_batch(self, record: int, key: str | None, raw: list[RawHit]) -> None:
        self.hit_count += len(raw)
        if self.truncated:
            return
        room = len(raw) if self.max_hits is None else min(len(raw), self.max_hits - len(self.hits))
        if room > 0:
            new = [_make_hit(record, key, h) for h in raw[:room]]
            if self.budget is not None:
                for j, hit in enumerate(new):
                    self.stored_bytes += _hit_cost(hit)
                    if self.stored_bytes > self.budget:  # the memory budget is used up
                        new = new[:j]
                        room = j
                        break
            self.hits.extend(new)
            if self.on_hits is not None:
                self._pending.extend(new)
        if room < len(raw):
            self.truncated = True

    def maybe_emit(self, force: bool = False) -> None:
        if self.on_hits is None:
            return
        counts = (self.hit_count, self.record_count)
        if not self._pending and counts == self._emitted_counts:
            return
        now = time.monotonic()
        if not force and self._last_emit is not None and now - self._last_emit < EMIT_INTERVAL:
            return
        batch = HitBatch(self._pending, hit_count=self.hit_count, record_count=self.record_count,
                         truncated=self.truncated, bytes_scanned=self.bytes_scanned)
        self._pending = []
        self._last_emit = now
        self._emitted_counts = counts
        self.on_hits(batch)


# ----------------------------------------------------------------------------------
# search
# ----------------------------------------------------------------------------------
@dataclass
class _ScanStats:
    records_scanned: int = 0
    records_parsed: int = 0
    bytes_scanned: int = 0
    used_bytes_mode: bool = False
    count_only: bool = False   # set by the consumer: only the number of hits matters now


_Checkpoint = None
"""Yielded by :func:`_scan` between records/blocks (consumers flush and emit there)."""


def _record_blocks(doc: Any, source: Any, block_bytes: int, progress: Progress | None,
                   cancel: CancelToken | None, wait_after: float | None) -> Iterator[Any]:
    """``doc.iter_blocks(source)``, except for a whole-document pass that starts while the
    index is still being built and could use byte needles (``wait_after`` not ``None``):
    the growing index is followed in parse mode for ``wait_after`` seconds (the first
    results appear at once); then the pass waits until the index is complete and goes on
    with the remaining records. Following longer would slow the indexer down to the pace
    of the parse-mode search -- both threads need the GIL -- while the indexer alone is
    many times faster and its completion enables byte mode for the rest of the file."""
    if wait_after is None or source is not None or doc.index_complete:
        yield from doc.iter_blocks(source, block_bytes=block_bytes, cancel=cancel)
        return
    deadline = time.perf_counter() + wait_after
    next_record = 0
    blocks = doc.iter_blocks(None, block_bytes=block_bytes, cancel=cancel)
    try:
        for blk in blocks:
            yield blk
            next_record = int(blk.indices[-1]) + 1
            if not doc.index_complete and time.perf_counter() >= deadline:
                break
        else:
            return
    finally:
        blocks.close()
    message = progress.message if progress is not None else ""
    if progress is not None:
        progress.set_message(t("search.progress.wait_index", name=doc.source_path.name))
    doc.build_index(cancel=cancel)  # waits for the running build (or builds the index itself)
    if progress is not None:
        progress.set_message(message)
    log.debug("search: index complete, continuing at record %d", next_record)
    yield from doc.iter_blocks(range(next_record, doc.record_count), block_bytes=block_bytes, cancel=cancel)


def _scan(doc: Any, opts: SearchOptions, source: Any, stats: _ScanStats, *, block_bytes: int,
          progress: Progress | None, cancel: CancelToken | None
          ) -> Iterator[tuple[int, str | None, list[RawHit] | _HitStream] | _RecordBatch | None]:
    """The search pass shared by :func:`search` and :func:`iter_hits`: yields
    ``(record, member key, raw hits)`` for every matching record in source order -- or,
    for records confirmed from their located tokens (byte mode), a :class:`_RecordBatch`
    of consecutive ones -- and ``None`` at checkpoints (after each block and every
    :data:`CANCEL_EVERY` records). ``raw hits`` is a non-empty list, or -- for streamed
    records and parsed records larger than :data:`INCREMENTAL_BYTES` -- a
    :class:`_HitStream` that the consumer must exhaust before resuming the scan (it may
    hold no hit at all). When the pass is complete, ``doc.ensure_unchanged()`` checks that
    the file was not changed meanwhile."""
    matcher = _Matcher(opts)
    # number values only matter when a number can be a hit (exact text via codec.loads)
    loads = codec.loads if matcher.numbers_on else _loads_ignoring_numbers
    if not doc.index_complete:
        if source is None:
            doc.start_index()  # followed below; returns the running build when there is one
        else:
            # a partial pass over an arbitrary source cannot be resumed: wait for (or build)
            # the index first
            doc.build_index(cancel=cancel)
    total = _source_total(doc, source)
    if progress is not None:
        if progress.total is None and total is not None:
            progress.set_total(total)
        if not progress.message:
            progress.set_message(t("search.progress.search", name=doc.source_path.name))
    # only a plain, non-translit query can get byte needles once the index is final
    may_use_needles = opts.query.mode == "plain" and not opts.query.translit
    finder: Any = None
    maxlen = 0
    needles_decided = False
    done = 0
    for blk in _record_blocks(doc, source, block_bytes, progress, cancel,
                              FOLLOW_SECONDS if may_use_needles else None):
        check(cancel)
        if not needles_decided and doc.index_complete:
            # the string flags are final now: byte needles (or None = parse every record)
            needles_decided = True
            needles = opts.query.prefilter_needles(doc.flags)
            if needles:
                finder = core.MultiFinder(needles)
                maxlen = max(len(x) for x in needles)
            log.debug("search %r: %s", opts.query.text,
                      f"byte prefilter with {len(needles)} needles" if needles else "parse mode")
        member = doc.member_records
        stats.records_scanned += len(blk.indices)
        if finder is not None:
            stats.used_bytes_mode = True
        if blk.huge:
            i = int(blk.indices[0])
            span = blk.span(0)
            if finder is None or _huge_contains(doc, span, finder, maxlen, cancel):
                stats.records_parsed += 1
                stream, key = _stream_hits(doc, matcher, i, span, member, cancel)
                yield i, key, stream
            done += span[1] - span[0]
        elif finder is not None and LOCATE:
            # byte mode: candidates, then the located tokens of each candidate
            data = blk.data
            candidates = yield from _byte_block(doc, matcher, finder, data, blk.spans, blk.indices, member, loads,
                                                cancel, stats)
            stats.records_parsed += candidates
            done += len(data)
        else:
            data = blk.data
            sp = array("Q")
            sp.frombytes(blk.spans)
            indices = blk.indices
            if finder is not None:  # byte mode, every candidate parsed (pure-Python core)
                cand = array("Q")
                cand.frombytes(finder.find_in_spans(data, blk.spans))
                ks: Iterable[int] = cand
            else:
                ks = range(len(indices))
            n = 0
            for k in ks:
                n += 1
                if n % CANCEL_EVERY == 0:
                    check(cancel)
                    yield _Checkpoint
                i = int(indices[k])
                buf = data[sp[2 * k]:sp[2 * k + 1]]
                stats.records_parsed += 1
                try:
                    value, key = _parse_record(buf, member, loads)
                except JsonDecodeError:
                    log.info("record %d cannot be parsed in memory; streaming it", i)
                    stream, key = _stream_hits(doc, matcher, i, doc.record_span(i), member, cancel)
                    yield i, key, stream
                    continue
                if len(buf) > INCREMENTAL_BYTES:  # a big record: its hits batch by batch
                    yield i, key, _HitStream(matcher.match_iter(value, key))
                    continue
                raw = matcher.match(value, key)
                if raw:
                    yield i, key, raw
            done += len(data)
        stats.bytes_scanned = done
        if progress is not None:
            progress.update(done)
        yield _Checkpoint
    check(cancel)
    ensure_unchanged = getattr(doc, "ensure_unchanged", None)
    if ensure_unchanged is not None:
        ensure_unchanged()
    if progress is not None:
        progress.finish()


def _confirm_record(doc: Any, matcher: _Matcher, i: int, buf: bytes, member: bool, loads: Callable[[bytes], Any],
                    cancel: CancelToken | None) -> Iterator[tuple[int, str | None, list[RawHit] | _HitStream]]:
    """The parse-and-walk confirm step of one record (at most one item, like the
    parse-mode loop of :func:`_scan`): streamed when the decoder refuses it, incremental
    when it is big."""
    try:
        value, key = _parse_record(buf, member, loads)
    except JsonDecodeError:
        log.info("record %d cannot be parsed in memory; streaming it", i)
        stream, key = _stream_hits(doc, matcher, i, doc.record_span(i), member, cancel)
        yield i, key, stream
        return
    if len(buf) > INCREMENTAL_BYTES:
        yield i, key, _HitStream(matcher.match_iter(value, key))
        return
    raw = matcher.match(value, key)
    if raw:
        yield i, key, raw


def _byte_block(doc: Any, matcher: _Matcher, finder: Any, data: bytes, spans: bytes, indices: Any, member: bool,
                loads: Callable[[bytes], Any], cancel: CancelToken | None, stats: _ScanStats | None = None
                ) -> Generator[tuple[int, str | None, list[RawHit] | _HitStream] | _RecordBatch | None, None, int]:
    """Byte mode over one block: ``find_in_spans`` gives the candidate records,
    ``MultiFinder.locate_tokens`` the keys/scalars of each candidate that hold a needle
    occurrence, and :meth:`_Matcher.locate_records` turns them into the records' hits
    (module docstring). Candidates larger than :data:`INCREMENTAL_BYTES`, records with
    duplicate keys and the other cases ``locate_records`` declines go through
    :func:`_confirm_record`. Yields like :func:`_scan` (the located records in
    :class:`_RecordBatch` items, count-only ones while ``stats.count_only``); returns the
    number of candidates."""
    cand = array("Q")
    cand.frombytes(finder.find_in_spans(data, spans))
    if not cand:
        return 0
    sp = array("Q")
    sp.frombytes(spans)
    bigs: list[int] = []
    small: Any = cand
    if len(data) > INCREMENTAL_BYTES and max(map(operator.sub, sp[1::2], sp[0::2])) > INCREMENTAL_BYTES:
        bigs = [k for k in cand if sp[2 * k + 1] - sp[2 * k] > INCREMENTAL_BYTES]
        small = array("Q", [k for k in cand if sp[2 * k + 1] - sp[2 * k] <= INCREMENTAL_BYTES])
    # the first LOCATE_HEAD candidates are located on their own: their hits are shown at once
    parts = [(c, spans[16 * c[0]:16 * c[-1] + 16] if c[-1] - c[0] + 1 == len(c)  # consecutive records
              else b"".join([spans[16 * k:16 * k + 16] for k in c]))
             for c in (small[:LOCATE_HEAD], small[LOCATE_HEAD:]) if c]
    locate_records = matcher.locate_records
    nb = len(bigs)
    bi = 0
    limit = 1
    for p, (cands, sub) in enumerate(parts):
        try:
            toks = finder.locate_tokens(data, sub, member)
        except ValueError:
            # a candidate is not valid JSON (changed file?): the confirm step decides, one by one
            log.debug("search: locate_tokens refused a block; confirming its candidates by parsing")
            toks = []
            bigs[bi:] = sorted(bigs[bi:] + list(cands))
            nb = len(bigs)
        nt = len(toks)
        ti = 0
        part_end = parts[p + 1][0][0] if p + 1 < len(parts) else len(indices)
        # the located records come in batches of at most CANCEL_EVERY records (the block's
        # first record alone), in order with the records confirmed by parsing; a
        # checkpoint after each
        while ti < nt or (bi < nb and bigs[bi] < part_end):
            next_big = bigs[bi] if bi < nb else len(indices)
            if ti < nt and cands[toks[ti][0]] < next_big:
                batch = _RecordBatch()
                ti, _, k = locate_records(data, toks, ti, cands, sp, indices, member, next_big, limit, batch,
                                          stats is not None and stats.count_only)
                limit = CANCEL_EVERY
                if batch.indices:
                    yield batch
            else:
                k = next_big
                bi += 1
            if k >= 0:
                yield from _confirm_record(doc, matcher, int(indices[k]), data[sp[2 * k]:sp[2 * k + 1]], member,
                                           loads, cancel)
            check(cancel)
            yield _Checkpoint
    while bi < nb:  # big candidates after the last located one
        k = bigs[bi]
        bi += 1
        yield from _confirm_record(doc, matcher, int(indices[k]), data[sp[2 * k]:sp[2 * k + 1]], member, loads,
                                   cancel)
        check(cancel)
        yield _Checkpoint
    return len(cand)


def _check_opts(opts: Any) -> None:
    if not isinstance(opts, SearchOptions):
        raise TypeError(f"opts must be SearchOptions, not {type(opts).__name__}")


def search(doc: Any, opts: SearchOptions, source: Any = None, *, max_hits: int | None = 100000,
           progress: Progress | None = None, cancel: CancelToken | None = None,
           on_hits: Callable[[HitBatch], Any] | None = None, block_bytes: int = BLOCK_BYTES) -> SearchResult:
    """Search ``doc`` (all records, or ``source``: a RecordSet, range, list or iterable of
    record numbers) for ``opts`` -- see the module docstring.

    ``max_hits``: hits stored in the result (``None`` = all); later hits are only counted
    (use :func:`iter_hits` to stream every hit, e.g. into :func:`export_hits`). The
    stored hits also stay within a quarter of ``Settings.memory_limit_bytes`` (estimated;
    ``truncated`` is set when the budget cut them short).
    ``on_hits(batch)``: called with newly stored hits while the search runs (from this
    thread). Progress in bytes; :class:`~jsonreshape.errors.Cancelled` on ``cancel``;
    :class:`~jsonreshape.errors.FileChangedError` when the file changed during the scan.
    """
    _check_opts(opts)
    if max_hits is not None and (isinstance(max_hits, bool) or int(max_hits) < 0):
        raise ValueError(f"max_hits must be >= 0 or None, got {max_hits!r}")
    max_hits = None if max_hits is None else int(max_hits)
    started = time.perf_counter()
    stats = _ScanStats()
    sink = _RecordSink(t("search.recordset_description", text=opts.query.text))
    settings = getattr(doc, "settings", None) or get_settings()
    col = _Collector(max_hits, on_hits, sink, budget=max(1, int(settings.memory_limit_bytes) // 4))
    scan = _scan(doc, opts, source, stats, block_bytes=block_bytes, progress=progress, cancel=cancel)
    try:
        for item in scan:
            if item is _Checkpoint:
                col.bytes_scanned = stats.bytes_scanned
                sink.flush()
                col.maybe_emit()
            elif type(item) is _RecordBatch:
                col.add_records(item)
                stats.count_only = col.truncated  # later hits are only counted: the scan may skip building them
            else:
                col.add(*item)
                stats.count_only = col.truncated
        col.bytes_scanned = stats.bytes_scanned
        col.maybe_emit(force=True)
        records = sink.finish()
    except BaseException:
        sink.abort()
        raise
    finally:
        scan.close()
    elapsed = time.perf_counter() - started
    mode = "bytes" if stats.used_bytes_mode else "parse"
    log.info("search %r in %s: %d hits in %d records, %s scanned in %.2f s (%s mode)", opts.query.text,
             doc.source_path.name, col.hit_count, col.record_count, format_bytes(stats.bytes_scanned), elapsed,
             mode)
    return SearchResult(hits=col.hits, hit_count=col.hit_count, record_count=col.record_count, records=records,
                        truncated=col.truncated, mode_used=mode, bytes_scanned=stats.bytes_scanned,
                        elapsed=elapsed, records_scanned=stats.records_scanned,
                        records_parsed=stats.records_parsed)


def iter_hits(doc: Any, opts: SearchOptions, source: Any = None, *, progress: Progress | None = None,
              cancel: CancelToken | None = None, block_bytes: int = BLOCK_BYTES) -> Iterator[SearchHit]:
    """Every hit of :func:`search` (same order, same values), produced lazily and never
    stored: memory stays bounded however many hits there are, e.g.
    ``export_hits(iter_hits(doc, opts), "hits.csv")``. Arguments are checked at once;
    progress, cancellation and errors happen while iterating."""
    _check_opts(opts)
    return _iter_hits(_scan(doc, opts, source, _ScanStats(), block_bytes=block_bytes, progress=progress,
                            cancel=cancel))


def _iter_hits(scan: Iterator[tuple[int, str | None, list[RawHit] | _HitStream] | _RecordBatch | None]
               ) -> Iterator[SearchHit]:
    try:
        for item in scan:
            if item is _Checkpoint:
                continue
            if type(item) is _RecordBatch:
                for i, key, raw in zip(item.indices, item.keys, item.raws):
                    for r in raw:
                        yield _make_hit(i, key, r)
                continue
            i, key, raw = item  # type: ignore[misc]
            for batch in _raw_batches(raw):
                for r in batch:
                    yield _make_hit(i, key, r)
    finally:
        scan.close()  # type: ignore[attr-defined]


def reference_search(doc: Any, opts: SearchOptions, source: Any = None) -> list[SearchHit]:
    """Brute force: parse every record and confirm it with the same function as
    :func:`search` (no prefilter, no cap). Huge records are loaded whole as well (up to
    ``Settings.memory_limit_bytes``, else :class:`TooLargeError`) so this stays the exact
    reference; meant for tests and diagnostics, not for multi-GB records."""
    _check_opts(opts)
    matcher = _Matcher(opts)
    if not doc.index_complete:
        doc.build_index()
    indices: Iterable[int] = range(doc.record_count) if source is None else _iter_indices(source)
    member = doc.member_records
    large = doc.settings.large_node_bytes
    limit = doc.settings.memory_limit_bytes
    out: list[SearchHit] = []
    handle = None
    try:
        for i in indices:
            i = int(i)
            s, e = doc.record_span(i)
            if e - s <= large:
                buf = doc.record_bytes(i)
            else:
                if e - s > limit:
                    raise TooLargeError(size=format_bytes(e - s))
                if handle is None:
                    handle = open_shared(doc.data_path, "rb")
                buf = read_range(handle, s, e)
            try:
                value, key = _parse_record(buf, member)
            except JsonDecodeError:
                stream, key = _stream_hits(doc, matcher, i, (s, e), member, None)
                raw = [r for batch in stream for r in batch]
            else:
                raw = matcher.match(value, key)
            out.extend(_make_hit(i, key, r) for r in raw)
    finally:
        if handle is not None:
            handle.close()
    return out


# ----------------------------------------------------------------------------------
# export
# ----------------------------------------------------------------------------------
_EXPORT_FLUSH = 1 * MiB


def export_hits(hits: Iterable[SearchHit], path: StrPath, fmt: str = "csv", *, progress: Progress | None = None,
                cancel: CancelToken | None = None, bom: bool | None = None, delimiter: str | None = None) -> int:
    """Write hits to ``path`` atomically and streaming; returns the number written.

    ``fmt="csv"``: columns ``record, key, path, where, text`` (``path`` dotted, ``$`` for
    the record itself; ``key`` empty for non-member records), UTF-8 with BOM (``bom``,
    default ``Settings.csv_bom``), ``delimiter`` (default ``Settings.csv_delimiter``),
    ``\\r\\n`` line ends; a lone surrogate (from a ``\\udXXX`` escape in the data) is
    written as its ``\\udXXX`` text. ``fmt="json"``: an array of objects
    ``{"record", "key", "path", "where", "text", "spans"}``, one per line. Progress in bytes
    written; :class:`~jsonreshape.errors.Cancelled` leaves ``path`` untouched.
    """
    kind = str(fmt).strip().lower().lstrip(".")
    if kind not in EXPORT_FORMATS:
        raise UserInputError("search.export.bad_format", fmt=str(fmt))
    if progress is not None and not progress.message:
        progress.set_message(t("search.progress.export", name=Path(os.fspath(path)).name))
    settings = get_settings()
    written = 0
    count = 0
    with atomic_write(path, "wb") as f:
        if kind == "csv":
            use_bom = settings.csv_bom if bom is None else bool(bom)
            delim = settings.csv_delimiter if delimiter is None else str(delimiter)
            buf = io.StringIO()
            writer = csv.writer(buf, delimiter=delim, lineterminator="\r\n")
            if use_bom:
                buf.write("\N{ZERO WIDTH NO-BREAK SPACE}")
            writer.writerow(CSV_COLUMNS)
            for hit in hits:
                if count % CANCEL_EVERY == 0:
                    check(cancel)
                writer.writerow((hit.record, hit.key if hit.key is not None else "", hit.path_text, hit.where,
                                 hit.text))
                count += 1
                if buf.tell() >= _EXPORT_FLUSH:
                    written += _flush_text(buf, f)
                    if progress is not None:
                        progress.update(written)
            written += _flush_text(buf, f)
        else:
            f.write(b"[")
            written += 1
            for hit in hits:
                if count % CANCEL_EVERY == 0:
                    check(cancel)
                piece = (b",\n" if count else b"\n") + codec.dumps(hit.to_dict())
                f.write(piece)
                written += len(piece)
                count += 1
                if progress is not None and count % CANCEL_EVERY == 0:
                    progress.update(written)
            tail = b"\n]\n" if count else b"]\n"
            f.write(tail)
            written += len(tail)
        check(cancel)
    if progress is not None:
        progress.update(written)
        progress.finish()
    log.info("exported %d search hits to %s (%s)", count, path, kind)
    return count


def _flush_text(buf: io.StringIO, f: Any) -> int:
    data = buf.getvalue().encode("utf-8", "backslashreplace")
    buf.seek(0)
    buf.truncate()
    f.write(data)
    return len(data)


# ----------------------------------------------------------------------------------
# history
# ----------------------------------------------------------------------------------
class SearchHistory:
    """Saved searches (AGENT_PROMPT §6.2 "Qidiruv tarixi saqlanadi").

    A JSON file (default ``app_home()/search_history.json``) holding entries
    ``{"text": str, "options": dict, "time": float}``, most recent first, at most
    ``max_entries`` (200). Adding a text that is already present moves it to the front with
    the new options (de-duplicated by the exact search text). A missing or corrupt file
    reads as an empty history. Writes are atomic; changes are made under
    :func:`jsonreshape.query.file_lock` (and a thread lock), so searches of several
    processes at the same time keep all their entries (the empty file
    ``search_history.json.lock`` stays next to it). The folder is created with
    :func:`jsonreshape.fileio.make_dirs` (long paths work); file-system errors are raised
    localized (:func:`jsonreshape.fileio.os_error`; a lock held too long:
    ``search.history_busy``).
    """

    def __init__(self, path: StrPath | None = None, *, max_entries: int = HISTORY_MAX) -> None:
        self.path = Path(path) if path is not None else app_home() / HISTORY_FILE
        self.max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        try:
            with open_shared(self.path, "rb") as f:
                raw = f.read(_HISTORY_MAX_BYTES + 1)
        except Exception as exc:  # missing (normal on first use) or unreadable
            if os.path.exists(long_path(self.path)):
                log.warning("cannot read search history %s: %s", self.path, exc)
            return []
        if len(raw) > _HISTORY_MAX_BYTES:
            log.warning("search history %s is too large; ignoring it", self.path)
            return []
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError) as exc:
            log.warning("search history %s is corrupt (%s); starting a new one", self.path, exc)
            return []
        items = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(items, list):
            log.warning("search history %s has an unexpected layout; ignoring it", self.path)
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text or text in seen:
                continue
            options = item.get("options")
            when = item.get("time")
            seen.add(text)
            out.append({
                "text": text,
                "options": options if isinstance(options, dict) else {},
                "time": float(when) if isinstance(when, (int, float)) and not isinstance(when, bool) else 0.0,
            })
        return out[: self.max_entries]

    def _save(self, entries: list[dict[str, Any]]) -> None:
        try:
            make_dirs(self.path.parent)
        except OSError as exc:
            raise os_error(exc, self.path.parent, writing=True) from exc
        payload = {"version": HISTORY_VERSION, "entries": entries}
        data = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8", "backslashreplace") + b"\n"
        with atomic_write(self.path, "wb") as f:
            f.write(data)

    def _file_lock(self) -> Any:
        from .query import file_lock  # (the filter module is not needed for searching)

        return file_lock(self.path, "search.history_busy")

    def list(self) -> list[dict[str, Any]]:
        """Entries, most recent first (copies)."""
        with self._lock:
            return self._load()

    def texts(self) -> list[str]:
        """Just the search texts, most recent first."""
        return [e["text"] for e in self.list()]

    def add(self, text: str, options: dict[str, Any] | None = None) -> None:
        """Record a search (blank texts are ignored). ``options`` must be JSON-serializable
        (e.g. :meth:`SearchOptions.to_dict`); ``TypeError`` otherwise."""
        if not isinstance(text, str):
            raise TypeError(f"search text must be str, not {type(text).__name__}")
        if not text.strip():
            return
        opts = dict(options) if options else {}
        try:
            json.dumps(opts)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"search history options must be JSON-serializable: {exc}") from None
        with self._lock, self._file_lock():
            entries = [e for e in self._load() if e["text"] != text]
            entries.insert(0, {"text": text, "options": opts, "time": time.time()})
            self._save(entries[: self.max_entries])

    def remove(self, text: str) -> bool:
        """Forget one search text; returns whether it was present."""
        with self._lock:
            if not any(e["text"] == text for e in self._load()):
                return False  # nothing to change: no lock (and no folder) needed
            with self._file_lock():
                entries = self._load()
                kept = [e for e in entries if e["text"] != text]
                if len(kept) == len(entries):
                    return False
                self._save(kept)
                return True

    def clear(self) -> None:
        """Forget every entry (the file is kept, empty)."""
        with self._lock, self._file_lock():
            self._save([])
