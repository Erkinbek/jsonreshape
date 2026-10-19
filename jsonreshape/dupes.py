"""Duplicate records by a key (docs/ARCHITECTURE.md §12.6, the ``dupes.py`` part; shared
with the stage-5 statistics and used by the bulk ``dedupe`` rule).

:func:`find_duplicates` ``(doc, key, source=None, *, keep="first", memory_limit=None,
progress=None, cancel=None)`` finds the records of ``source`` whose *key* value occurs more
than once and returns a :class:`DuplicateReport`: the number of groups, the duplicates
(every record of a group except the kept one), the records with and without the key, the
duplicates as an ascending :class:`~jsonreshape.recordset.RecordSet` of positions and the
first groups as examples. It works on any document -- edited or not -- through the public
Document API only (``pinned``, ``iter_blocks``, ``member_records``, ``record_key``,
``record_span``, :func:`jsonreshape.stream.stream_values`), so the positions are the
effective positions of the pinned state (§12.0).

The key
-------
* a concrete path inside the record value (:func:`jsonreshape.paths.parse_pattern`
  syntax: ``id``, ``contact.phone``, ``tags[0]``, ``["a.b"]``, ``/x/0``); ``$`` = the whole
  record value. Wildcards (``*``, ``[*]``, ``..``) are refused (``dupes.error.wildcard``),
  an empty key too (``dupes.error.key``);
* ``"$key"``: the member key of the records of keyed / wrapped-object documents. On other
  documents every record counts as *missing* (like ``$key`` in sort and filter) and the
  report carries the warning ``dupes.warn.no_record_keys``; no record data is read then.

Dict semantics apply on the way to the value (the last of duplicate keys wins), like
``core.PathExtractor``, ``paths.get`` and ``stream_values``. Records without the key never
count (not as a group member, not as a duplicate); ``null`` is a value like any other.

Equality (canonical JSON)
-------------------------
Numbers are equal when their exact decimal values are (``1 == 1.0 == 1.00 == 1e0 ==
10e-1``, ``-0 == 0``, ``1e400 != 2e400``, integers of any length exact -- never through a
float); strings exactly (no case folding, no Unicode normalization; escapes decoded, so
``"\\u0041" == "A"``); ``true``, ``false`` and ``null`` are distinct values of their own
types (``1 != true``, ``"1" != 1``, ``null != "null"``); objects ignore the order of their
members (after dict semantics for duplicate keys), arrays keep their order. Each key value
is turned into a canonical byte string (strings: their UTF-8 bytes, lone surrogates with
``surrogatepass``; numbers: sign, significant digits and the exponent of the exact decimal
value; containers: a self-delimiting encoding with object members sorted by key) and
hashed with blake2b-128. Two values are taken as equal when their hashes are: the chance
that two different values share a hash is about ``n**2 / 2**129`` (``< 1e-20`` for a
billion records), so no confirmation pass is made (documented decision).

Algorithm and memory
--------------------
Pass 1 reads the records block by block. The key value comes from ``core.PathExtractor``
(the raw token of the value; strategy ``"extract"``, the default with the Rust core) or
from ``codec.loads`` of the record (strategy ``"parse"``, the default with the pure-Python
core, whose reference extractor is slower than parsing; both give identical results -- the
extract strategy hashes strings without escapes and plain positive integers (ids) straight from
their tokens, with the same canonical bytes); ``$key`` from ``split_member``. Every record with
a key becomes ONE Python int
``(hash << 64) | position`` (an entry; estimated at :data:`ENTRY_BYTES` = 60 bytes: the
52-byte int plus its list slot). While the entries fit in ``memory_limit`` (default
``Settings.memory_limit_bytes // 4``; an explicit value is capped at half of
``memory_limit_bytes``, the sort's share) they stay in a list; beyond it the list is sorted
(``recordset._sorted_pieces``: slices, so no single C call holds the GIL for long) and
written as a run of 24-byte big-endian entries to a temporary directory
``appdirs.work_dir(settings.cache_dir)/sort-<pid>-<hex>/`` (the sort's naming, so
``recordset.purge_stale_files`` removes what a crashed process left behind). A run holds at
least :data:`MIN_RUN_ENTRIES` entries (a tiny limit would otherwise write thousands of
files). Pass 2 merges the runs and the sorted in-memory rest with ``heapq.merge`` (more
than :data:`MAX_FANIN` runs are first merged into bigger runs): equal hashes are adjacent
and, inside a group, positions ascend. Every member of a group except the first (``keep=
"first"``) or the last (``keep="last"``) -- in POSITION order, whatever the order of
``source`` -- gets its bit set in a ``bytearray`` bitmap of ``record_count`` bits, which
finally yields the ascending RecordSet (``RecordSetBuilder``). The temporary directory is
always removed (success, error, cancellation).

Besides the entries, the bitmap takes ``record_count / 8`` bytes, and a second one of the
same size marks the positions already seen when ``source`` may repeat positions (a list,
an array, a RecordSet that is not ascending): a position given twice is examined once.

Huge records (larger than ``Settings.large_node_bytes``) are never loaded: the key value
is read with :func:`jsonreshape.stream.stream_values` within
:func:`jsonreshape.stream.capture_budget` (``$key``: ``doc.record_key``, which reads only
the member head). A record whose key value exceeds the budget, or whose key cannot be
decoded at all, is *skipped*: it is counted (``DuplicateReport.skipped``, warnings
``dupes.warn.too_large`` / ``dupes.warn.unreadable`` naming the first one) and never marked
as a duplicate. A record whose key value cannot be decoded in memory (e.g. nested deeper
than the decoder allows) is streamed the same way first. The ijson deviations of
:mod:`jsonreshape.stream` apply to streamed keys only: ``-0`` and exponent spellings do not
matter here (exact values), but a lone surrogate escape (``"\\ud800"``) inside a STREAMED
key string arrives as ``"?"`` and so differs from the same key in a record read in memory.

Progress is reported in bytes: the index build (when the index is not complete yet: the
first part of the bar), the records read (``iter_blocks``) and the merge (the last tenth
of the bar, in proportion to the entries merged). Cancellation is checked between blocks,
every :data:`CHECK_RECORDS` records, for every sorted slice and run write and every
:data:`MERGE_BATCH` merged entries. When the scan has finished, ``doc.ensure_unchanged()``
makes sure the file was not changed meanwhile (``FileChangedError``).

Pinned documents and scopes (§12.3)
-----------------------------------
:func:`find_duplicates` begins with ``doc = doc.pinned()``, so a task never sees a state
change mid-run. A ``source`` carrying ``gen`` (a RecordSet made from an earlier state) must
belong to the pinned state: ``source.gen in (None, doc.gen)``, else
``EditError("edits.error.stale_scope")``. The result RecordSet is tagged with ``gen =
doc.gen`` (0 for a document without an overlay state).

Decisions (documented deviations / extensions of §12.6)
-------------------------------------------------------
* ``keep`` refers to position order (the spec's ``(hash << 64) | position`` entries);
  "first" keeps the record with the smallest position of each group.
* :class:`DuplicateReport` has, besides the spec's fields, ``key``, ``keep``, ``scanned``,
  ``skipped``, ``example_sizes`` (the size of each example group: the examples list at
  most :data:`EXAMPLE_POSITIONS` positions), ``gen``, ``runs``, ``elapsed`` and localized
  ``warnings`` (:meth:`DuplicateReport.warning_texts`).
* Examples: the :data:`EXAMPLE_GROUPS` groups whose first record comes first (by
  position), each with its first :data:`EXAMPLE_POSITIONS` positions (ascending) and the
  key text = the compact JSON text (``codec.dumps``) of the key value of the group's first
  record, cut to :data:`EXAMPLE_TEXT_CHARS` characters (read again after the merge: at
  most 20 records). When that record is huge, its value comes from ijson, which spells
  some numbers its own way (``1e0`` -> ``1``, ``0e5`` -> ``0E+5``: same value).
* Extra keyword arguments ``strategy`` (``"auto"``/``"extract"``/``"parse"``) and
  ``block_bytes`` (like ``filter_records`` / ``sort_records``).
* The canonical form is public for reuse (stage-5 statistics): :func:`canonical_bytes`
  (Python values), :func:`canonical_token` (raw JSON tokens, without decoding scalars) and
  :func:`key_hash` (the 128-bit hash).
* The sorted-slice sort, list release, file removal and warning texts are the record-set
  module's helpers (``recordset._sorted_pieces`` / ``_release`` / ``_remove_file`` /
  ``_remove_tree`` / ``_warning_texts``), reused so both external sorts behave alike.

Stage-5 extensions (docs/ARCHITECTURE.md §14.2; backward compatible)
---------------------------------------------------------------------
* ``keep="none"`` marks EVERY member of a group of two or more records ("show all the
  duplicated records"; ``DuplicateReport.duplicates`` then counts every member). The bulk
  ``dedupe`` rule keeps accepting only ``first`` / ``last`` (``_check_keep`` without
  ``allow_none``): deleting every copy is never offered by a rule that says "remove the
  duplicates".
* ``top=N`` (``find_duplicates``): the N largest groups (size descending, then first position
  ascending) with their first :data:`EXAMPLE_POSITIONS` positions and key texts
  (``DuplicateReport.top_groups`` / ``top_sizes``); at most :data:`TOP_MAX`. ``examples=N``:
  how many groups ``DuplicateReport.examples`` lists (default :data:`EXAMPLE_GROUPS`).
* :func:`iter_groups` -- the generic walk over sorted ``(hash << 64) | position`` entries: one
  tuple ``(hash, first, records, occurrences, positions)`` per group, optional marking of the
  members in a bitmap by ``keep``; the duplicate finder, the stage-5 statistics (exact mode) and
  the merge dedupe use it.
* :class:`KeyHasher` (was ``_KeyReader``) and :class:`HashRuns` (was ``_Runs``) are public
  helpers (the old names stay as aliases): the key pass with its strategies, skip/missing
  accounting and spilling, and the sorted runs on disk. ``KeyHasher(..., offset=, entries=)``
  builds entries over a position space shared by several documents.
* :func:`find_duplicates_many` ``(docs, key, *, keep="first", ...)`` finds the duplicates across
  several documents at once (positions are global: input ``i`` occupies ``offsets[i] ..
  offsets[i] + counts[i] - 1``; "first" = the earliest input, then the smallest position) and
  returns a :class:`MultiDuplicateReport` whose :meth:`~MultiDuplicateReport.keep_source` gives
  the ascending positions of one input that are NOT duplicates (a record source for
  ``Document.iter_blocks`` / ``writer.export``: ``len``, ``[]``, iteration, ``iter_chunks``,
  ``gen``). Every input is pinned; per-input warnings name the file.
"""

from __future__ import annotations

import bisect
import hashlib
import heapq
import itertools
import json
import logging
import os
import re
import secrets
import sys
import time
from array import array
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import codec, core
from .appdirs import work_dir
from .errors import Cancelled, EditError, JsonReshapeError, UserInputError
from .fileio import long_path, os_error
from .i18n import t
from .paths import MISSING, PathPattern, get as path_get, parse_pattern
from .reader import split_member
from .recordset import (
    RecordSet,
    RecordSetBuilder,
    _release,
    _remove_file,
    _remove_tree,
    _sorted_pieces,
    _warning_texts,
)
from .settings import get_settings
from .stream import CaptureLimitExceeded, capture_budget, stream_values
from .tasks import CancelToken, Progress, check, format_bytes

log = logging.getLogger(__name__)

__all__ = [
    "CHECK_RECORDS",
    "DuplicateReport",
    "ENTRY_BYTES",
    "EXAMPLE_GROUPS",
    "EXAMPLE_POSITIONS",
    "EXAMPLE_TEXT_CHARS",
    "HashRuns",
    "KEEP_CHOICES",
    "KEY_FIELD",
    "KeepSource",
    "KeyHasher",
    "MAX_FANIN",
    "MERGE_BATCH",
    "MIN_RUN_ENTRIES",
    "MultiDuplicateReport",
    "PositionSource",
    "TOP_MAX",
    "canonical_bytes",
    "canonical_token",
    "find_duplicates",
    "find_duplicates_many",
    "iter_groups",
    "key_hash",
    "parse_key",
]

KiB = 1 << 10
MiB = 1 << 20

KEY_FIELD = "$key"           # the member key of keyed / wrapped-object records
KEEP_CHOICES = ("first", "last", "none")   # "none": every member of a group is marked (stage 5)
TOP_MAX = 10000              # largest groups that find_duplicates(top=...) may list
EXAMPLE_GROUPS = 20          # groups listed in DuplicateReport.examples
EXAMPLE_POSITIONS = 20       # positions listed per example group
EXAMPLE_TEXT_CHARS = 120     # key texts of the examples are cut to this many characters
ENTRY_BYTES = 60             # estimated memory of one entry: a 192-bit int (52 B) + its list slot (8 B)
RUN_ENTRY_BYTES = 24         # one entry in a run file: 16-byte hash + 8-byte position, big-endian
MIN_RUN_ENTRIES = 4096       # a spilled run holds at least this many entries
MAX_FANIN = 64               # runs merged at once (more: intermediate merge passes)
CHECK_RECORDS = 4096         # records between cancellation / memory checks in pass 1
MERGE_BATCH = 16384          # entries per merge batch (cancellation + progress; ~1 MB of ints)
RUN_WRITE_ITEMS = 8192       # entries joined by one write of a run
RUN_READ_BYTES = 1 * MiB     # read buffers of the runs being merged, together (64 KiB .. 1 MiB each)
RUN_READ_MIN = 64 * KiB
RUN_WRITE_BYTES = 1 * MiB
LOGGED_PROBLEMS = 10         # unreadable keys logged as warnings (the rest at debug level)
SCAN_END = 0.9               # the scan fills the bar up to here; the merge does the rest
INDEX_SHARE = 0.45           # share of the bar for an index build done here

_POS_MASK = (1 << 64) - 1
_NONZERO = re.compile(rb"[^\x00]+")
_BIT_OFFSETS = tuple(tuple(k for k in range(8) if b >> k & 1) for b in range(256))
_DECODE_ERRORS = (codec.JsonDecodeError, ValueError, RecursionError, UnicodeDecodeError)
_BIG_ENDIAN = sys.byteorder == "big"
_WS = b" \t\r\n"
_ELLIPSIS = "…"

# test hook: the PathExtractor class used by the "extract" strategy (None = core.PathExtractor)
_EXTRACTOR: Any = None


# ----------------------------------------------------------------------------------
# canonical form and hash
# ----------------------------------------------------------------------------------
_NUMBER_RE = re.compile(rb"(-?)(0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?")
_MAX_INT_DIGITS = 4000       # int() of longer digit strings is refused by Python's default limit
_scanstring = json.decoder.scanstring
_H_EMPTY = hashlib.blake2b(digest_size=16)
_H_STRING = hashlib.blake2b(b"s", digest_size=16)   # top-level strings: b"s" + UTF-8
_from_bytes = int.from_bytes


def _big_int(text: bytes) -> int:
    """``int(text)`` for a decimal text of any length (Python refuses more than ~4300
    digits; a pathological exponent can be that long)."""
    if len(text) <= _MAX_INT_DIGITS:
        return int(text)
    neg = text[:1] == b"-"
    digits = text.lstrip(b"+-")
    value = 0
    for i in range(0, len(digits), _MAX_INT_DIGITS):
        chunk = digits[i:i + _MAX_INT_DIGITS]
        value = value * 10 ** len(chunk) + int(chunk)
    return -value if neg else value


def _number_canon(tok: bytes) -> bytes:
    """Canonical bytes of a JSON number token (its exact decimal value):
    ``b"n0"`` for zero (any sign), else ``b"n" + sign + digits + b"e" + hex(exponent) +
    b";"`` with value ``digits * 10**exponent`` and ``digits`` without leading or
    trailing zeros. ``ValueError`` when ``tok`` is not a JSON number."""
    m = _NUMBER_RE.fullmatch(tok)
    if m is None:
        raise ValueError(f"invalid JSON number token: {bytes(tok[:40])!r}")
    neg, ip, fp, ep = m.groups()
    digits = ip + fp if fp else ip
    lead = digits.lstrip(b"0")
    if not lead:
        return b"n0"
    sig = lead.rstrip(b"0")
    exp = len(lead) - len(sig) - (len(fp) if fp else 0)
    if ep:
        exp += _big_int(ep)
    return b"".join((b"n-" if neg else b"n+", sig, b"e%x;" % exp))


def _number_value_canon(v: Any) -> bytes:
    """Canonical bytes of a Python number as :func:`codec.loads` / ``stream_values``
    produce them (``JsonFloat`` / ``JsonInt`` by their exact token)."""
    cls = v.__class__
    if cls is int:
        return _number_canon(int.__repr__(v).encode("ascii"))
    if cls is float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"not a JSON number: {v!r}")
        return _number_canon(float.__repr__(v).encode("ascii"))
    raw = getattr(v, "raw", None)
    if isinstance(raw, str):  # codec.JsonFloat / codec.JsonInt
        return _number_canon(raw.encode("ascii"))
    if isinstance(v, Decimal):
        if not v.is_finite():
            raise ValueError(f"not a JSON number: {v!r}")
        return _number_canon(str(v).encode("ascii"))
    if isinstance(v, int):
        return _number_canon(int.__repr__(int(v)).encode("ascii"))
    if isinstance(v, float):
        return _number_value_canon(float(v))
    raise TypeError(f"not a JSON value: {type(v).__name__}")


def _string_bytes(s: str) -> bytes:
    return s.encode("utf-8", "surrogatepass")


def _container_canon(value: Any) -> bytes:
    """Self-delimiting canonical bytes of a container (iterative: any depth).

    ``[`` items ``]``; ``{`` (``k<len>:<key>`` value)* ``}`` with members sorted by their
    UTF-8 key bytes; nested strings ``s<len>:<utf-8>``; numbers as :func:`_number_canon`;
    ``z`` / ``t`` / ``f`` for null / true / false."""
    out = bytearray()
    stack: list[Any] = [value]
    pop = stack.pop
    push = stack.append
    while stack:
        v = pop()
        cls = v.__class__
        if cls is bytes:          # a marker pushed below (closing bracket, member key)
            out += v
        elif cls is str:
            b = _string_bytes(v)
            out += b"s%d:" % len(b)
            out += b
        elif v is None:
            out += b"z"
        elif v is True:
            out += b"t"
        elif v is False:
            out += b"f"
        elif isinstance(v, dict):
            out += b"{"
            push(b"}")
            members = sorted(((_string_bytes(k), item) for k, item in v.items()), key=_first, reverse=True)
            for kb, item in members:          # reversed: the smallest key is popped first
                push(item)
                push(b"k%d:" % len(kb) + kb)
        elif isinstance(v, (list, tuple)):
            out += b"["
            push(b"]")
            for item in reversed(v):
                push(item)
        elif isinstance(v, str):
            b = _string_bytes(str.__str__(v))
            out += b"s%d:" % len(b)
            out += b
        else:
            out += _number_value_canon(v)
    return bytes(out)


def _first(pair: tuple[bytes, Any]) -> bytes:
    return pair[0]


def canonical_bytes(value: Any) -> bytes:
    """The canonical bytes of a JSON value given as Python data (``codec.loads`` /
    ``stream_values`` types; module docstring "Equality"). Two values are equal JSON values
    exactly when their canonical bytes are equal. ``TypeError`` / ``ValueError`` for data
    that is not a JSON value."""
    cls = value.__class__
    if cls is str:
        return b"s" + _string_bytes(value)
    if value is None:
        return b"z"
    if value is True:
        return b"t"
    if value is False:
        return b"f"
    if isinstance(value, (dict, list, tuple)):
        return _container_canon(value)
    if isinstance(value, str):
        return b"s" + _string_bytes(str.__str__(value))
    return _number_value_canon(value)


def canonical_token(raw: bytes | bytearray | memoryview) -> bytes:
    """:func:`canonical_bytes` of the value of one JSON token (the raw bytes of a valid
    JSON value; surrounding whitespace is tolerated) -- without decoding scalars:
    ``canonical_token(t) == canonical_bytes(codec.loads(t))``. Errors of
    :data:`_DECODE_ERRORS` for invalid or undecodable input."""
    if raw.__class__ is not bytes:
        raw = bytes(raw)
    if not raw:
        raise ValueError("empty JSON token")
    c = raw[0]
    if c == 0x22:                                    # string
        if 0x5C not in raw:
            if raw[-1] != 0x22 or len(raw) < 2:
                raise ValueError(f"invalid JSON string token: {raw[:40]!r}")
            return b"s" + raw[1:-1]
        text = raw.decode("utf-8")
        value, end = _scanstring(text, 1)
        if end != len(text):
            raise ValueError(f"invalid JSON string token: {raw[:40]!r}")
        return b"s" + _string_bytes(value)
    if c == 0x74 and raw == b"true":
        return b"t"
    if c == 0x66 and raw == b"false":
        return b"f"
    if c == 0x6E and raw == b"null":
        return b"z"
    if c == 0x7B or c == 0x5B:                       # object / array
        value = codec.loads(raw)
        if not isinstance(value, (dict, list)):     # pragma: no cover - loads checks the whole text
            raise ValueError(f"invalid JSON container token: {raw[:40]!r}")
        return _container_canon(value)
    if c in _WS:
        stripped = raw.strip(_WS)
        if stripped:
            return canonical_token(stripped)
        raise ValueError("empty JSON token")
    if raw.isdigit() and (c != 0x30 or len(raw) == 1):     # a plain non-negative integer (ids)
        if c == 0x30:
            return b"n0"
        sig = raw.rstrip(b"0")
        return b"".join((b"n+", sig, b"e%x;" % (len(raw) - len(sig))))
    return _number_canon(raw)


def key_hash(canon: bytes) -> int:
    """The 128-bit hash of canonical bytes (blake2b, digest size 16) as an int."""
    h = _H_EMPTY.copy()
    h.update(canon)
    return _from_bytes(h.digest(), "big")


def _value_text(value: Any) -> str:
    """Compact JSON text of a key value for the examples, cut to EXAMPLE_TEXT_CHARS."""
    try:
        text = codec.dumps(value).decode("utf-8", "replace")
    except Exception:  # pragma: no cover - values come from codec / stream
        text = repr(value)
    if len(text) > EXAMPLE_TEXT_CHARS:
        text = text[:EXAMPLE_TEXT_CHARS - 1] + _ELLIPSIS
    return text


def _u64(data: bytes | bytearray | memoryview) -> array:
    a = array("Q")
    a.frombytes(data)
    if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only
        a.byteswap()
    return a


# ----------------------------------------------------------------------------------
# the report
# ----------------------------------------------------------------------------------
@dataclass
class DuplicateReport:
    """Result of :func:`find_duplicates`.

    ``groups``: key values that occur in more than one record; ``duplicates``: records
    marked as duplicates (every member of a group except the kept one -- with
    ``keep="none"`` every member; ``== len(records)``); ``records_with_key``: records whose
    key exists (``null`` counts); ``missing``: records without the key; ``records``: the
    duplicates' positions, ascending (``RecordSet`` of kind ``"duplicates"``, tagged with
    ``gen``); ``examples``: ``(key text, positions)`` of the first :data:`EXAMPLE_GROUPS`
    groups (module docstring).

    Extensions: ``key`` (the key as given), ``keep``, ``scanned`` (records examined =
    ``records_with_key + missing + skipped``), ``skipped`` (records whose key could not be
    read: never marked), ``example_sizes`` (records in each example group), ``gen`` (state
    gen of the positions; ``None`` without edit support), ``runs`` (sorted runs written to
    disk; 0 = in memory), ``elapsed`` (seconds), ``warnings`` / ``warning_params`` (i18n
    keys; texts: :meth:`warning_texts`), ``top_groups`` / ``top_sizes`` (the ``top`` largest
    groups as ``(key text, first positions)``, size descending then first position
    ascending; empty unless ``top`` was given). :meth:`close` releases the RecordSet.
    """

    groups: int
    duplicates: int
    records_with_key: int
    missing: int
    records: RecordSet
    examples: list[tuple[str, list[int]]] = field(default_factory=list)
    key: str = ""
    keep: str = "first"
    scanned: int = 0
    skipped: int = 0
    example_sizes: list[int] = field(default_factory=list)
    gen: int | None = None
    runs: int = 0
    elapsed: float = 0.0
    warnings: tuple[str, ...] = ()
    warning_params: tuple[dict[str, Any], ...] = ()
    top_groups: list[tuple[str, list[int]]] = field(default_factory=list)
    top_sizes: list[int] = field(default_factory=list)

    def warning_texts(self) -> list[str]:
        """The warnings in the current UI language (``<key>.one`` for a count of 1, counts
        with grouped digits)."""
        return _warning_texts(self.warnings, self.warning_params)

    def close(self) -> None:
        """Release the RecordSet (its file, if any)."""
        self.records.close()


# ----------------------------------------------------------------------------------
# argument checks
# ----------------------------------------------------------------------------------
def _parse_key(key: Any) -> tuple[str, tuple | None]:
    """``(key text, path)``; path ``None`` for ``$key``."""
    if not isinstance(key, str) or not key.strip():
        raise UserInputError("dupes.error.key", value=repr(key))
    if key.strip() == KEY_FIELD:
        return KEY_FIELD, None
    pattern = parse_pattern(key)            # PathSyntaxError (localized) for bad syntax
    if not pattern.is_concrete:
        raise UserInputError("dupes.error.wildcard", key=pattern.text)
    return pattern.text, tuple(pattern.segments)


def parse_key(key: Any) -> tuple[str, tuple | None]:
    """The key of the duplicate finder (also the diff ``--key`` and the merge ``--dedupe-by``):
    ``(normalized text, path)`` -- ``path`` the concrete path inside the record (``()`` for
    ``$``, the whole record) or ``None`` for ``$key`` (the member key). ``UserInputError``
    (``dupes.error.key`` / ``dupes.error.wildcard``) or ``PathSyntaxError`` for a bad key."""
    return _parse_key(key)


def _check_keep(keep: Any, allow_none: bool = False) -> str:
    """``"first"`` / ``"last"`` (and ``"none"`` when ``allow_none``: the finders; the bulk
    ``dedupe`` rule never deletes every copy) -- ``UserInputError("dupes.error.keep")``."""
    value = keep.strip().lower() if isinstance(keep, str) else None
    choices = KEEP_CHOICES if allow_none else KEEP_CHOICES[:2]
    if value not in choices:
        raise UserInputError("dupes.error.keep", value=repr(keep),
                             choices=t("dupes.keep.choices3" if allow_none else "dupes.keep.choices2"))
    return value


def _check_top(top: Any, message_key: str = "dupes.error.top") -> int:
    if isinstance(top, bool) or not isinstance(top, int) or not 0 <= top <= TOP_MAX:
        raise UserInputError(message_key, value=repr(top), max=TOP_MAX)
    return int(top)


def _memory_limit(memory_limit: Any, settings: Any) -> int:
    total = int(settings.memory_limit_bytes)
    own = memory_limit if memory_limit is not None else max(1, total // 4)
    if isinstance(own, bool) or not isinstance(own, int) or own < 1:
        raise UserInputError("dupes.error.memory_limit", value=repr(memory_limit))
    cap = max(1, total // 2)
    if own > cap:
        log.info("dupes: memory limit %s lowered to %s (half of the memory limit setting)", format_bytes(own),
                 format_bytes(cap))
    return min(own, cap)


def _check_strategy(strategy: str) -> str:
    if strategy not in ("auto", "extract", "parse"):
        raise ValueError(f"unknown strategy {strategy!r} (auto, extract or parse)")
    if strategy == "auto":
        return "extract" if core.BACKEND == "rust" else "parse"
    return strategy


def _check_scope(source: Any, gen: Any) -> None:
    """``source.gen`` must be ``None`` or the pinned state's gen (§12.3)."""
    src_gen = getattr(source, "gen", None) if source is not None else None
    if src_gen is not None and src_gen != gen:
        raise EditError("edits.error.stale_scope")


def _normalize_source(doc: Any, source: Any) -> Any:
    """``None`` for all records (also the document's base set), else ``source``."""
    if isinstance(source, RecordSet) and source._is_base_of(doc):
        return None
    return source


def _unique_source(src: Any) -> bool:
    """The source cannot name a position twice (no "seen" bitmap needed)."""
    if src is None or isinstance(src, range):
        return True
    return isinstance(src, RecordSet) and src.is_ascending()


def _iter_positions(src: Any) -> Iterator[int]:
    """The positions of a source (like ``Document.iter_blocks`` reads them)."""
    if isinstance(src, (list, tuple, array, range)):
        yield from src
    elif hasattr(src, "iter_chunks"):
        for chunk in src.iter_chunks():
            yield from chunk
    elif hasattr(src, "__len__") and hasattr(src, "__getitem__") and not isinstance(src, (dict, str)):
        for k in range(len(src)):
            yield src[k]
    else:
        yield from src


def _ensure_unchanged(doc: Any) -> None:
    ensure = getattr(doc, "ensure_unchanged", None)
    if ensure is not None:
        ensure()


def _set_gen(rs: RecordSet, gen: Any) -> None:
    """Tag the result with the state gen its positions belong to (§12.3)."""
    rs.gen = gen


# ----------------------------------------------------------------------------------
# progress
# ----------------------------------------------------------------------------------
class _Bar:
    """Byte progress: [index build] -> records read -> merge (module docstring)."""

    def __init__(self, progress: Progress | None, total: int, message: str) -> None:
        self.p = progress
        if progress is not None:
            if not progress.message:
                progress.set_message(message)
            if progress.total is None:
                progress.set_total(max(1, total))
        self.scan_start = 0.0

    def index(self) -> Progress | None:
        """A sub-progress for the index build (the first part of the bar)."""
        self.scan_start = INDEX_SHARE
        return self.p.child(0.0, INDEX_SHARE) if self.p is not None else None

    def scan(self, total: int | None) -> Progress | None:
        if self.p is None:
            return None
        return self.p.child(self.scan_start, SCAN_END, total=total)

    def merge(self, entries: int, message: str) -> Progress | None:
        if self.p is None:
            return None
        self.p.set_message(message)
        return self.p.child(SCAN_END, 1.0, total=max(1, entries))

    def finish(self) -> None:
        if self.p is not None:
            self.p.finish()


def _data_bytes(doc: Any) -> int:
    """Bytes of all records: the data file without its BOM, or -- edited documents (§12.3)
    -- the effective records' bytes (``doc.effective_bytes()``)."""
    if doc.edited:
        return max(1, int(doc.effective_bytes()))
    return max(1, int(doc.data_size) - int(doc.info.data_start))


def _scan_total(doc: Any, src: Any, full: int) -> int | None:
    """Expected bytes of the records read (exact for all records, else proportional)."""
    if src is None:
        return full
    try:
        n = len(src)
    except TypeError:
        return None
    count = int(doc.record_count)
    if count <= 0:
        return None
    return max(1, int(full * min(1.0, n / count)))


# ----------------------------------------------------------------------------------
# sorted runs on disk
# ----------------------------------------------------------------------------------
def _iter_run(f: Any, bufsize: int) -> Iterator[int]:
    """Entries of a run file (24-byte big-endian ints), in order."""
    rest = b""
    while True:
        buf = f.read(bufsize)
        if not buf:
            if rest:
                raise ValueError("truncated duplicate-finder run file")
            return
        if rest:
            buf = rest + buf
        cut = len(buf) - len(buf) % RUN_ENTRY_BYTES
        rest = buf[cut:]
        yield from [_from_bytes(buf[i:i + RUN_ENTRY_BYTES], "big") for i in range(0, cut, RUN_ENTRY_BYTES)]


class HashRuns:
    """Sorted runs of entries in a temporary directory (created on the first spill).

    Entries are non-negative ints below ``2**192`` (24 big-endian bytes in a run file), e.g.
    ``(key_hash << 64) | position``; any producer may use other packings that fit (the diff
    stores ``(a_position << 64) | b_position``). :meth:`spill` sorts a list and writes it as
    one run; :meth:`merged` yields every entry in ascending order (the runs merged with the
    sorted in-memory rest; more than ``fan_in`` runs are merged in passes first). The
    directory ``<directory>/sort-<pid>-<hex>`` follows the record-set sort's naming, so
    ``recordset.purge_stale_files`` removes what a crashed process left behind; :meth:`cleanup`
    (or leaving a ``with`` block) removes it. Cancellation is checked for every sorted slice,
    run write and merge pass."""

    def __init__(self, directory: Path, cancel: CancelToken | None, fan_in: int | None = None) -> None:
        self.directory = directory
        self.cancel = cancel
        self.fan_in = max(2, int(MAX_FANIN if fan_in is None else fan_in))
        self.dir: Path | None = None
        self.paths: list[Path] = []
        self.written = 0
        self.merge_passes = 0
        self._files: list[Any] = []

    def _new_path(self) -> Path:
        if self.dir is None:
            for _ in range(100):
                d = self.directory / f"sort-{os.getpid()}-{secrets.token_hex(4)}"
                try:
                    os.mkdir(long_path(d))
                    break
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise os_error(exc, d, writing=True) from exc
            else:  # pragma: no cover - 100 name collisions
                raise JsonReshapeError("recordset.error.write", path=str(self.directory), detail="no temporary name")
            self.dir = d
        self.written += 1
        return self.dir / f"run-{self.written:06d}.bin"

    def _write(self, items: Iterable[int]) -> Path:
        p = self._new_path()
        it = iter(items)
        try:
            with open(long_path(p), "wb", buffering=RUN_WRITE_BYTES) as f:
                while True:
                    check(self.cancel)
                    chunk = list(itertools.islice(it, RUN_WRITE_ITEMS))
                    if not chunk:
                        break
                    f.write(b"".join([e.to_bytes(RUN_ENTRY_BYTES, "big") for e in chunk]))
        except OSError as exc:
            raise os_error(exc, p, writing=True) from exc
        return p

    def spill(self, entries: list[int]) -> None:
        """Sort ``entries`` (consumed) and write them as one run."""
        pieces = _sorted_pieces(entries, cancel=self.cancel)
        try:
            self.paths.append(self._write(itertools.chain.from_iterable(pieces)))
        finally:
            pieces.close()      # cancelled: the generator empties its lists now
            _release(entries)

    def _open(self, paths: list[Path]) -> list[Iterator[int]]:
        share = max(RUN_READ_MIN, RUN_READ_BYTES // max(1, len(paths)))
        bufsize = max(RUN_ENTRY_BYTES, share - share % RUN_ENTRY_BYTES)   # whole entries per read
        iters = []
        for p in paths:
            try:
                f = open(long_path(p), "rb", buffering=0)  # noqa: SIM115 - closed in _close_files()
            except OSError as exc:
                raise os_error(exc, p) from exc
            self._files.append(f)
            iters.append(_iter_run(f, bufsize))
        return iters

    def _close_files(self) -> None:
        files, self._files = self._files, []
        for f in files:
            try:
                f.close()
            except OSError:  # pragma: no cover
                pass

    def reduce(self, extra: int) -> None:
        """Merge runs ``fan_in`` at a time until one merge of the runs and ``extra`` more
        inputs needs at most ``fan_in`` files."""
        while len(self.paths) + extra > self.fan_in:
            check(self.cancel)
            group = self.paths[:self.fan_in]
            iters = self._open(group)
            try:
                merged = self._write(heapq.merge(*iters))
            finally:
                self._close_files()
            for p in group:
                _remove_file(p)
            self.paths = self.paths[self.fan_in:] + [merged]
            self.merge_passes += 1

    def merged(self, entries: list[int]) -> Iterator[int]:
        """Every entry in sorted order: the runs merged with the sorted in-memory
        ``entries`` (consumed). Close the generator when stopping early."""
        pieces = _sorted_pieces(entries, cancel=self.cancel)
        try:
            if not self.paths:
                for piece in pieces:
                    yield from piece
                return
            self.reduce(1)
            iters = self._open(self.paths)
            iters.insert(0, itertools.chain.from_iterable(pieces))
            yield from heapq.merge(*iters)
        finally:
            pieces.close()
            _release(entries)
            self._close_files()

    def cleanup(self) -> None:
        """Close the run files and remove the temporary directory (never raises)."""
        self._close_files()
        d, self.dir = self.dir, None
        self.paths = []
        if d is not None:
            _remove_tree(d)

    def __enter__(self) -> HashRuns:
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()


_Runs = HashRuns     # the stage-3 name (edits.py imports it)


# ----------------------------------------------------------------------------------
# pass 1: reading the keys
# ----------------------------------------------------------------------------------
class KeyHasher:
    """Reads the key of every record of the blocks it is given and collects the entries
    ``(hash << 64) | (offset + position)``, spilling sorted runs to ``runs`` whenever
    ``max_entries`` are held (module docstring "Algorithm and memory").

    ``path``: the concrete key path inside the record (``()`` = the record itself) or
    ``None`` for ``$key`` (see :func:`parse_key`); ``strategy``: ``"extract"`` (core
    PathExtractor) or ``"parse"`` (``codec.loads``), never ``"auto"``; ``seen``: a bitmap of
    the positions examined before (sources that may repeat positions) or ``None``;
    ``offset``: added to every position in the entries (several documents in one position
    space, :func:`find_duplicates_many`); ``entries``: the list to append to (shared by the
    hashers of several documents; :attr:`entries` is replaced by a new list after a spill).

    Feed it the blocks of ``doc.iter_blocks`` with :meth:`block`; it counts ``with_key``,
    ``missing`` (no value at the path, ``$key`` on a document without member records) and
    the skipped records (``too_large``: a streamed key value over the capture budget,
    ``unreadable``; ``first_too_large`` / ``first_unreadable`` are local positions).
    :meth:`value_of` / :meth:`stream_value` read a record's key value as Python data."""

    def __init__(self, doc: Any, path: tuple | None, strategy: str, settings: Any, cancel: CancelToken | None,
                 max_entries: int, runs: HashRuns, seen: bytearray | None = None, *, offset: int = 0,
                 entries: list[int] | None = None) -> None:
        if strategy not in ("extract", "parse"):
            raise ValueError(f"unknown strategy {strategy!r} (extract or parse)")
        self.doc = doc
        self.path = path                         # None: $key
        self.pattern = PathPattern(path) if path is not None else None
        self.member = bool(doc.member_records)
        self.cancel = cancel
        self.budget = capture_budget(settings)
        self.max_entries = max(1, int(max_entries))
        self.runs = runs
        self.seen = seen
        self.offset = int(offset)
        self.entries: list[int] = entries if entries is not None else []
        self.with_key = 0
        self.missing = 0
        self.too_large = 0
        self.unreadable = 0
        self.first_too_large: int | None = None
        self.first_unreadable: int | None = None
        self.peak = 0
        self.extractor: Any = None
        if path is not None and strategy == "extract":
            cls = _EXTRACTOR if _EXTRACTOR is not None else core.PathExtractor
            self.extractor = cls([self.pattern.to_core()], member_spans=self.member)  # type: ignore[union-attr]

    # -- bookkeeping ---------------------------------------------------------------------
    def _maybe_spill(self) -> None:
        n = len(self.entries)
        if n > self.peak:
            self.peak = n
        if n >= self.max_entries:
            entries, self.entries = self.entries, []
            self.runs.spill(entries)

    def _add(self, pos: int, canon: bytes) -> None:
        self.entries.append((key_hash(canon) << 64) | (pos + self.offset))
        self.with_key += 1

    def _repeated(self, idx: array) -> set[int] | None:
        """Block offsets of positions examined before (sources that may repeat them)."""
        seen = self.seen
        if seen is None:
            return None
        rep: set[int] | None = None
        for k, pos in enumerate(idx):
            b = pos >> 3
            bit = 1 << (pos & 7)
            if seen[b] & bit:
                if rep is None:
                    rep = set()
                rep.add(k)
            else:
                seen[b] |= bit
        return rep

    def _skip_too_large(self, pos: int) -> None:
        self.too_large += 1
        if self.first_too_large is None:
            self.first_too_large = pos

    def _skip_unreadable(self, pos: int, exc: BaseException) -> None:
        self.unreadable += 1
        if self.first_unreadable is None:
            self.first_unreadable = pos
        if self.unreadable <= LOGGED_PROBLEMS:
            log.warning("dupes: the key of record #%d cannot be read: %s", pos, exc)
        else:
            log.debug("dupes: the key of record #%d cannot be read: %s", pos, exc)

    # -- blocks ------------------------------------------------------------------------
    def block(self, blk: Any) -> None:
        rep = self._repeated(blk.indices)
        if blk.huge:
            if not rep:
                self._huge(int(blk.indices[0]), blk.span(0))
        elif self.path is None:
            self._block_keys(blk, rep)
        elif self.extractor is not None:
            self._block_extract(blk, rep)
        else:
            self._block_parse(blk, rep)
        self._maybe_spill()

    def _block_extract(self, blk: Any, rep: set[int] | None) -> None:
        """The extractor runs over :data:`CHECK_RECORDS` records at a time: its output
        (32 bytes per record) stays small even for a block of millions of tiny records."""
        data = blk.data
        idx = blk.indices
        spans = blk.spans
        extract = self.extractor.extract
        h_string = _H_STRING
        h_empty = _H_EMPTY
        find = data.find
        off = self.offset
        n = len(idx)
        for lo in range(0, n, CHECK_RECORDS):
            check(self.cancel)
            self._maybe_spill()
            hi = min(n, lo + CHECK_RECORDS)
            try:
                quads = _u64(extract(data, spans[16 * lo:16 * hi]))
            except ValueError:
                self._extract_each(blk, rep, lo, hi)   # invalid JSON in some record: one at a time
                continue
            nrec = hi - lo - (sum(1 for k in rep if lo <= k < hi) if rep else 0)
            found = 0        # records with a match (one quad per record: the path is concrete)
            streamed = 0     # ... whose value had to be streamed (counted by _streamed)
            append = self.entries.append
            for j in range(0, len(quads), 4):
                k = lo + quads[j]
                if rep and k in rep:
                    continue
                found += 1
                s = quads[j + 2]
                e = quads[j + 3]
                pos = idx[k]
                c = data[s]
                if c == 0x22:
                    if find(b"\\", s, e) < 0:                    # a string without escapes
                        h = h_string.copy()
                        h.update(data[s + 1:e - 1])
                        append((_from_bytes(h.digest(), "big") << 64) | (pos + off))
                        continue
                elif 0x31 <= c <= 0x39:
                    tok = data[s:e]
                    if tok.isdigit():          # a plain positive integer (ids): canonical_token inline
                        sig = tok.rstrip(b"0")
                        h = h_empty.copy()
                        h.update(b"n+" + sig + b"e%x;" % (e - s - len(sig)))
                        append((_from_bytes(h.digest(), "big") << 64) | (pos + off))
                        continue
                try:
                    canon = canonical_token(data[s:e])
                except _DECODE_ERRORS:
                    streamed += 1
                    self._streamed(pos, None)
                    continue
                append((key_hash(canon) << 64) | (pos + off))
            self.with_key += found - streamed
            self.missing += nrec - found

    def _extract_each(self, blk: Any, rep: set[int] | None, lo: int, hi: int) -> None:
        """Records ``lo .. hi-1`` of ``blk`` one by one (a record the extractor refuses is
        streamed)."""
        data = blk.data
        spans = blk.spans
        idx = blk.indices
        for k in range(lo, hi):
            if rep and k in rep:
                continue
            pos = idx[k]
            try:
                quads = _u64(self.extractor.extract(data, spans[16 * k:16 * k + 16]))
            except ValueError as exc:
                log.debug("dupes: extraction failed for record #%d (%s); streaming it", pos, exc)
                self._streamed(int(pos), None)
                continue
            if not quads:
                self.missing += 1
                continue
            try:
                canon = canonical_token(data[quads[2]:quads[3]])
            except _DECODE_ERRORS:
                self._streamed(int(pos), None)
                continue
            self._add(pos, canon)

    def _block_parse(self, blk: Any, rep: set[int] | None) -> None:
        data = blk.data
        idx = blk.indices
        sp = _u64(blk.spans)
        member = self.member
        path = self.path
        loads = codec.loads
        for k in range(len(idx)):
            if rep and k in rep:
                continue
            if k % CHECK_RECORDS == 0:
                check(self.cancel)
                self._maybe_spill()
            raw = data[sp[2 * k]:sp[2 * k + 1]]
            pos = idx[k]
            try:
                if member:
                    raw = raw[split_member(raw)[1]:]
                value = loads(raw)
            except _DECODE_ERRORS:
                self._streamed(int(pos), None)
                continue
            v = path_get(value, path) if path else value  # type: ignore[arg-type]
            if v is MISSING:
                self.missing += 1
                continue
            try:
                canon = canonical_bytes(v)
            except (TypeError, ValueError) as exc:  # pragma: no cover - codec values are JSON values
                self._skip_unreadable(int(pos), exc)
                continue
            self.entries.append((key_hash(canon) << 64) | (pos + self.offset))
            self.with_key += 1

    def _block_keys(self, blk: Any, rep: set[int] | None) -> None:
        idx = blk.indices
        if not self.member:
            self.missing += len(idx) - (len(rep) if rep else 0)
            return
        data = blk.data
        sp = _u64(blk.spans)
        for k in range(len(idx)):
            if rep and k in rep:
                continue
            if k % CHECK_RECORDS == 0:
                check(self.cancel)
                self._maybe_spill()
            pos = idx[k]
            try:
                key = split_member(data[sp[2 * k]:sp[2 * k + 1]])[0]
            except ValueError as exc:
                self._skip_unreadable(int(pos), exc)
                continue
            self.entries.append((key_hash(b"s" + _string_bytes(key)) << 64) | (pos + self.offset))
            self.with_key += 1

    # -- streamed records ----------------------------------------------------------------
    def _huge(self, pos: int, span: tuple[int, int]) -> None:
        if self.path is None:
            if not self.member:
                self.missing += 1
                return
            key = self.doc.record_key(pos)      # reads only the member head
            if key is None:  # pragma: no cover - member documents always have keys
                self.missing += 1
                return
            self._add(pos, b"s" + _string_bytes(key))
            return
        self._streamed(pos, span)

    def stream_value(self, pos: int, span: tuple[int, int] | None) -> Any:
        """The key value of record ``pos`` read with ijson (``MISSING`` when absent).
        Raises ``CaptureLimitExceeded`` and ``JsonReshapeError("stream.error.parse")``."""
        if span is None:
            span = self.doc.record_span(pos)
        got = stream_values(self.doc, span, [self.pattern], self.member, max_bytes=self.budget, first_only=True,
                            cancel=self.cancel)
        values = got.get(0) or []
        return values[0] if values else MISSING

    def _streamed(self, pos: int, span: tuple[int, int] | None) -> None:
        """Key of record ``pos`` by streaming it (huge records, decoding fallbacks)."""
        try:
            value = self.stream_value(pos, span)
        except CaptureLimitExceeded:
            self._skip_too_large(pos)
            return
        except Cancelled:
            raise
        except JsonReshapeError as exc:
            if exc.key != "stream.error.parse":
                raise          # file changed, damaged edit layer, ...: not a property of the record
            self._skip_unreadable(pos, exc)
            return
        if value is MISSING:
            self.missing += 1
            return
        try:
            canon = canonical_bytes(value)
        except (TypeError, ValueError) as exc:  # pragma: no cover - stream values are JSON values
            self._skip_unreadable(pos, exc)
            return
        self._add(pos, canon)

    # -- example texts -----------------------------------------------------------------
    def value_of(self, blk: Any, k: int) -> Any:
        """The key value of record ``k`` of ``blk`` as Python data (``MISSING`` when
        absent; ``$key``: the member key)."""
        pos = int(blk.indices[k])
        if self.path is None:
            if not self.member:
                return MISSING
            if blk.huge:
                key = self.doc.record_key(pos)
                return MISSING if key is None else key
            s, e = blk.span(k)
            return split_member(blk.data[s:e])[0]
        if blk.huge:
            return self.stream_value(pos, blk.span(0))
        s, e = blk.span(k)
        raw = blk.data[s:e]
        try:
            if self.member:
                raw = raw[split_member(raw)[1]:]
            value = codec.loads(raw)
        except _DECODE_ERRORS:
            return self.stream_value(pos, None)
        return path_get(value, self.path) if self.path else value


_KeyReader = KeyHasher     # the stage-3 name


# ----------------------------------------------------------------------------------
# pass 2: grouping
# ----------------------------------------------------------------------------------
_MARK = {"first": 1, "last": 2, "none": 3}


def iter_groups(entries: Iterable[int], *, min_records: int = 1, positions: int = 0,
                bits: bytearray | None = None, keep: str = "first", cancel: CancelToken | None = None,
                progress: Progress | None = None) -> Iterator[tuple[int, int, int, int, list[int] | None]]:
    """Walk SORTED entries ``(hash << 64) | position`` (equal hashes adjacent, positions
    ascending inside a run of equal hashes -- what :meth:`HashRuns.merged` yields) and yield one
    tuple per group of equal hashes ``(hash, first, records, occurrences, positions)``:

    * ``first``: the smallest position; ``records``: distinct positions (a position repeated
      in a row -- a value occurring twice in one record, a source naming a record twice --
      counts once); ``occurrences``: entries;
    * ``positions``: with ``positions=N > 0`` the first N distinct positions (ascending),
      else ``None``.

    Only groups with at least ``min_records`` records are yielded (``min_records=2``: the
    duplicate groups). With ``bits`` (a bitmap over the positions) the members of every group
    of two or more records are marked by ``keep``: ``"first"`` every member but the first,
    ``"last"`` every member but the last, ``"none"`` every member -- whatever is yielded.
    Cancellation is checked every :data:`MERGE_BATCH` entries; ``progress.update(entries
    walked)``."""
    if isinstance(min_records, bool) or not isinstance(min_records, int) or min_records < 1:
        raise ValueError(f"min_records must be an int >= 1, not {min_records!r}")
    mode = 0
    if bits is not None:
        if keep not in _MARK:
            raise ValueError(f"unknown keep {keep!r} (first, last or none)")
        mode = _MARK[keep]
    want = max(0, int(positions))
    mask = _POS_MASK
    it = iter(entries)
    prev_h = -1
    first = prev_pos = -1
    size = occ = 0
    gpos: list[int] | None = None
    done = 0
    while True:
        batch = list(itertools.islice(it, MERGE_BATCH))
        if not batch:
            break
        check(cancel)
        for e in batch:
            h = e >> 64
            pos = e & mask
            if h != prev_h:
                if size >= min_records:
                    yield prev_h, first, size, occ, (gpos if gpos is not None else ([first] if want else None))
                prev_h = h
                first = prev_pos = pos
                size = occ = 1
                gpos = None
                continue
            occ += 1
            if pos == prev_pos:
                continue
            size += 1
            if mode:
                if mode == 1:
                    p = pos
                elif mode == 2:
                    p = prev_pos
                else:
                    if size == 2:
                        bits[first >> 3] |= 1 << (first & 7)   # type: ignore[index]
                    p = pos
                bits[p >> 3] |= 1 << (p & 7)                    # type: ignore[index]
            if want:
                if size == 2:
                    gpos = [first, pos] if want > 1 else [first]
                elif size <= want:
                    gpos.append(pos)                              # type: ignore[union-attr]
            prev_pos = pos
        done += len(batch)
        del batch
        if progress is not None:
            progress.update(done)
    if size >= min_records:
        yield prev_h, first, size, occ, (gpos if gpos is not None else ([first] if want else None))


def _offer(heap: list[tuple[int, list[int], int]], first: int, positions: list[int], size: int,
           limit: int = EXAMPLE_GROUPS) -> None:
    """Keep the ``limit`` groups with the smallest first positions (a max-heap on ``-first``;
    first positions are unique, so the lists are never compared)."""
    if limit <= 0:
        return
    item = (-first, positions, size)
    if len(heap) < limit:
        heapq.heappush(heap, item)
    elif first < -heap[0][0]:
        heapq.heapreplace(heap, item)


def _offer_top(heap: list[tuple[int, int, list[int]]], top: int, first: int, positions: list[int],
               size: int) -> None:
    """Keep the ``top`` largest groups (a min-heap on ``(size, -first)``: of two groups of the
    same size the one with the smaller first position wins; first positions are unique)."""
    item = (size, -first, positions)
    if len(heap) < top:
        heapq.heappush(heap, item)
    elif (size, -first) > heap[0][:2]:
        heapq.heapreplace(heap, item)


def _walk_groups(stream: Iterator[int], keep: str, bits: bytearray, cancel: CancelToken | None,
                 prog: Progress | None, top: int = 0, examples: int = EXAMPLE_GROUPS
                 ) -> tuple[int, int, list[tuple[int, list[int], int]], list[tuple[int, list[int], int]]]:
    """Mark the duplicates of the sorted entries in ``bits`` (by ``keep``); returns ``(groups,
    duplicates, examples, top groups)``: the first ``examples`` groups ``(first position,
    positions, size)`` sorted by first position, top groups likewise sorted by size descending,
    then first position."""
    groups = dups = 0
    heap: list[tuple[int, list[int], int]] = []
    theap: list[tuple[int, int, list[int]]] = []
    every = keep == "none"
    for _h, first, size, _occ, gpos in iter_groups(stream, min_records=2, positions=EXAMPLE_POSITIONS, bits=bits,
                                                     keep=keep, cancel=cancel, progress=prog):
        groups += 1
        dups += size if every else size - 1
        _offer(heap, first, gpos, size, examples)   # type: ignore[arg-type]
        if top:
            _offer_top(theap, top, first, gpos, size)   # type: ignore[arg-type]
    listed = sorted(((-neg, positions, n) for neg, positions, n in heap), key=_first_int)
    tops = [(-neg, positions, n) for n, neg, positions in sorted(theap, key=_top_order)]
    return groups, dups, listed, tops


def _top_order(item: tuple[int, int, list[int]]) -> tuple[int, int]:
    return -item[0], -item[1]


def _group(stream: Iterator[int], keep_last: bool, bits: bytearray, cancel: CancelToken | None,
           prog: Progress | None) -> tuple[int, int, list[tuple[int, list[int], int]]]:
    """Walk the sorted entries: mark the duplicates in ``bits``; returns ``(groups,
    duplicates, examples)`` with examples ``(first position, positions, size)`` sorted by
    first position (the stage-3 entry point; :func:`_walk_groups` has the keep modes)."""
    groups, dups, examples, _ = _walk_groups(stream, "last" if keep_last else "first", bits, cancel, prog)
    return groups, dups, examples


def _first_int(item: tuple[int, list[int], int]) -> int:
    return item[0]


def _bits_to_set(bits: bytearray, builder: RecordSetBuilder, cancel: CancelToken | None) -> int:
    """Append the positions of the set bits (ascending) to ``builder``; returns their number."""
    offsets = _BIT_OFFSETS
    out = array("Q")
    total = 0
    n_runs = 0
    for m in _NONZERO.finditer(bits):
        n_runs += 1
        if n_runs % CHECK_RECORDS == 0:
            check(cancel)
        i = m.start()
        for b in m.group():
            base = i << 3
            for off in offsets[b]:
                out.append(base + off)
            i += 1
        if len(out) >= MERGE_BATCH:
            check(cancel)
            builder.extend(out)
            total += len(out)
            out = array("Q")
    if out:
        builder.extend(out)
        total += len(out)
    return total


# ----------------------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------------------
def find_duplicates(doc: Any, key: str, source: Any = None, *, keep: str = "first", memory_limit: int | None = None,
                    progress: Progress | None = None, cancel: CancelToken | None = None, strategy: str = "auto",
                    block_bytes: int = 8 * MiB, top: int = 0, examples: int = EXAMPLE_GROUPS) -> DuplicateReport:
    """Records of ``source`` whose ``key`` value occurs more than once (module docstring).

    ``key``: a concrete path inside the record (``$`` = the record itself) or ``"$key"``
    (member key); ``source``: ``None`` (all records), a RecordSet (``gen`` must match the
    document's), a ``range``, a list / array or any iterable of positions; ``keep``:
    ``"first"`` or ``"last"`` (position order), or ``"none"`` (every member of a group is a
    duplicate); ``memory_limit``: bytes for the in-memory entries (default
    ``Settings.memory_limit_bytes // 4``); ``top``: list the ``top`` largest groups too
    (``DuplicateReport.top_groups``, at most :data:`TOP_MAX`); ``examples``: how many groups
    ``DuplicateReport.examples`` lists (0 .. :data:`TOP_MAX`). Raises ``UserInputError`` for
    bad arguments, ``EditError("edits.error.stale_scope")`` for a source of another state,
    ``Cancelled`` (temporary files removed) and ``FileChangedError``.
    """
    started = time.perf_counter()
    doc = doc.pinned()
    key_text, path = _parse_key(key)
    keep = _check_keep(keep, allow_none=True)
    top = _check_top(top)
    examples = _check_top(examples, "dupes.error.examples")
    strategy = _check_strategy(strategy)
    settings = getattr(doc, "settings", None) or get_settings()
    limit = _memory_limit(memory_limit, settings)
    gen = doc.gen
    _check_scope(source, gen)
    src = _normalize_source(doc, source)
    name = doc.source_path.name
    data_bytes = _data_bytes(doc)
    bar = _Bar(progress, data_bytes, t("dupes.progress.scan", name=name))
    if not doc.index_complete:
        doc.build_index(progress=bar.index(), cancel=cancel)
    check(cancel)
    count = int(doc.record_count)
    member = bool(doc.member_records)
    seen = None if _unique_source(src) else bytearray((count + 7) >> 3)
    runs = HashRuns(work_dir(settings.cache_dir), cancel)
    reader = KeyHasher(doc, path, strategy, settings, cancel, max(MIN_RUN_ENTRIES, limit // ENTRY_BYTES), runs,
                       seen)
    builder = RecordSetBuilder("duplicates", t("dupes.desc", key=key_text))
    rs: RecordSet | None = None
    try:
        try:
            # -- pass 1: the keys ------------------------------------------------------
            scan_p = bar.scan(_scan_total(doc, src, data_bytes))
            if path is None and not member:
                _count_without_keys(reader, src, count, seen, cancel)
                if scan_p is not None:
                    scan_p.finish()
            else:
                _scan_blocks(doc, src, reader, block_bytes, scan_p, cancel)
            check(cancel)
            _ensure_unchanged(doc)   # results of a file another program changed are not reported
            # -- pass 2: the groups ----------------------------------------------------
            n_entries = reader.with_key
            merge_p = bar.merge(n_entries, t("dupes.progress.merge", name=name))
            bits = bytearray((count + 7) >> 3)
            entries, reader.entries = reader.entries, []
            stream = runs.merged(entries)
            try:
                groups, dups, listed, tops = _walk_groups(stream, keep, bits, cancel, merge_p, top, examples)
            finally:
                stream.close()
            del entries
            marked = _bits_to_set(bits, builder, cancel)
            del bits
            if marked != dups:  # pragma: no cover - would be a bug
                raise RuntimeError(f"duplicate finder: {marked} marked records for {dups} duplicates")
            rs = builder.finish()
            _set_gen(rs, gen)
            if merge_p is not None:
                merge_p.finish()
            # -- the examples' key texts -----------------------------------------------
            texts = _example_texts(doc, reader, sorted({ex[0] for ex in listed} | {tp[0] for tp in tops}),
                                   cancel)
        finally:
            runs.cleanup()
            _release(reader.entries)
    except BaseException:
        builder.abort()
        if rs is not None:
            rs.close()
        raise
    bar.finish()
    skipped = reader.too_large + reader.unreadable
    scanned = reader.with_key + reader.missing + skipped
    warnings: list[str] = []
    params: list[dict[str, Any]] = []
    if path is None and not member:
        if scanned:
            warnings.append("dupes.warn.no_record_keys")
            params.append({"count": scanned})
    elif scanned and reader.with_key == 0 and reader.missing == scanned:
        warnings.append("dupes.warn.key_absent")
        params.append({"key": key_text, "count": scanned})
    if reader.too_large:
        warnings.append("dupes.warn.too_large")
        params.append({"count": reader.too_large, "record": reader.first_too_large,
                       "size": format_bytes(reader.budget)})
    if reader.unreadable:
        warnings.append("dupes.warn.unreadable")
        params.append({"count": reader.unreadable, "record": reader.first_unreadable})
    elapsed = time.perf_counter() - started
    report = DuplicateReport(
        groups=groups, duplicates=dups, records_with_key=reader.with_key, missing=reader.missing, records=rs,
        examples=[(texts.get(first, ""), positions) for first, positions, _ in listed], key=key_text, keep=keep,
        scanned=scanned, skipped=skipped, example_sizes=[size for _, _, size in listed], gen=gen,
        runs=runs.written, elapsed=elapsed, warnings=tuple(warnings), warning_params=tuple(params),
        top_groups=[(texts.get(first, ""), positions) for first, positions, _ in tops],
        top_sizes=[size for _, _, size in tops])
    log.info("duplicates of %s by %s: %d groups, %d duplicates, %d with the key, %d missing, %d skipped "
             "(%.2f s, %d runs, peak %d entries)", name, key_text, groups, dups, reader.with_key, reader.missing,
             skipped, elapsed, runs.written, reader.peak)
    return report


def _scan_blocks(doc: Any, src: Any, reader: KeyHasher, block_bytes: int, progress: Progress | None,
                 cancel: CancelToken | None) -> None:
    """Pass 1 over the records of ``src`` (a function of its own: the last block -- up to
    ``block_bytes`` -- is released before the merge starts)."""
    blocks = doc.iter_blocks(src, block_bytes=block_bytes, progress=progress, cancel=cancel)
    try:
        for blk in blocks:
            check(cancel)
            reader.block(blk)
    finally:
        blocks.close()     # stops a read-ahead at once on errors and cancellation


def _count_without_keys(reader: KeyHasher, src: Any, count: int, seen: bytearray | None,
                        cancel: CancelToken | None) -> None:
    """``$key`` on a document without member records: every record is missing (no data is
    read; the positions of an explicit source are checked and counted once)."""
    if src is None:
        reader.missing += count
        return
    n = 0
    for pos in _iter_positions(src):
        n += 1
        if n % CHECK_RECORDS == 0:
            check(cancel)
        pos = int(pos)
        if pos < 0 or pos >= count:
            raise IndexError(f"record {pos} out of range ({count} records)")
        if seen is not None:
            b = pos >> 3
            bit = 1 << (pos & 7)
            if seen[b] & bit:
                continue
            seen[b] |= bit
        reader.missing += 1


def _example_texts(doc: Any, reader: KeyHasher, positions: list[int], cancel: CancelToken | None) -> dict[int, str]:
    """Key texts of the records at ``positions`` (the examples' first records)."""
    out: dict[int, str] = {}
    if not positions:
        return out
    blocks = doc.iter_blocks(sorted(positions), cancel=cancel)
    try:
        for blk in blocks:
            for k in range(len(blk.indices)):
                pos = int(blk.indices[k])
                try:
                    value = reader.value_of(blk, k)
                except Cancelled:
                    raise
                except (CaptureLimitExceeded, *_DECODE_ERRORS) as exc:  # pragma: no cover - read before
                    log.debug("dupes: example text of #%d not available: %s", pos, exc)
                    continue
                except JsonReshapeError as exc:
                    if exc.key != "stream.error.parse":
                        raise
                    continue
                if value is not MISSING:
                    out[pos] = _value_text(value)
    finally:
        blocks.close()
    return out


# ----------------------------------------------------------------------------------
# positions of a bitmap as a record source (find_duplicates_many)
# ----------------------------------------------------------------------------------
_SELECT_BLOCK = 1024           # bytes per block of the select index (8192 positions)
_POPCOUNT = bytes(bin(i).count("1") for i in range(256))


def _bits_range(bits: bytes | bytearray, offset: int, count: int) -> int:
    """The bits ``offset .. offset + count - 1`` of a bitmap (bit ``k`` of byte ``b`` is
    position ``8 * b + k``) as an int whose bit 0 is position ``offset``."""
    if count <= 0:
        return 0
    lo = offset >> 3
    hi = (offset + count + 7) >> 3
    value = int.from_bytes(bits[lo:hi], "little") >> (offset & 7)
    return value & ((1 << count) - 1)


class PositionSource:
    """Ascending positions ``0 .. count-1`` of ONE input whose bit is set (``keep=False``) or
    clear (``keep=True``) in a global bitmap -- a record source for
    :meth:`~jsonreshape.reader.Document.iter_blocks` / ``writer.export`` without an
    8-byte-per-record list: ``len()``, ``[k]`` (``k``-th position; negative ``k`` counts from
    the end), iteration, :meth:`iter_chunks` (``array('Q')`` pieces) and ``gen`` (the state gen
    of the positions: a writer refuses the source for another state). Holds one bit per record
    of the input."""

    def __init__(self, bits: bytes | bytearray, offset: int, count: int, *, keep: bool,
                 gen: int | None = None) -> None:
        count = max(0, int(count))
        value = _bits_range(bits, offset, count)
        if keep:
            value ^= (1 << count) - 1
        self.count = count
        self.gen = gen
        self._bits = value.to_bytes((count + 7) >> 3, "little")
        self._n = value.bit_count()
        cum = array("Q")
        acc = 0
        data = self._bits
        for b in range(0, len(data), _SELECT_BLOCK):
            cum.append(acc)
            acc += int.from_bytes(data[b:b + _SELECT_BLOCK], "little").bit_count()
        self._cum = cum

    def __len__(self) -> int:
        return self._n

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._n} of {self.count} positions>"

    def is_ascending(self) -> bool:
        return True

    def __getitem__(self, k: int) -> int:
        n = self._n
        if isinstance(k, bool) or not isinstance(k, int):
            raise TypeError(f"position index must be an int, not {type(k).__name__}")
        if k < 0:
            k += n
        if not 0 <= k < n:
            raise IndexError(f"position index {k} out of range ({n} positions)")
        block = bisect.bisect_right(self._cum, k) - 1
        rest = k - self._cum[block]
        data = self._bits
        b = block * _SELECT_BLOCK
        while True:
            c = _POPCOUNT[data[b]]
            if rest < c:
                return (b << 3) + _BIT_OFFSETS[data[b]][rest]
            rest -= c
            b += 1

    def iter_chunks(self, size: int = 65536) -> Iterator[array]:
        """The positions in ascending order, as ``array('Q')`` pieces of about ``size``."""
        data = self._bits
        offsets = _BIT_OFFSETS
        out = array("Q")
        for m in _NONZERO.finditer(data):
            for b in range(m.start(), m.end()):
                base = b << 3
                out.extend([base + k for k in offsets[data[b]]])
                if len(out) >= size:
                    yield out
                    out = array("Q")
        if out:
            yield out

    def __iter__(self) -> Iterator[int]:
        for chunk in self.iter_chunks():
            yield from chunk


class KeepSource(PositionSource):
    """The ascending positions of one input that are NOT duplicates
    (:meth:`MultiDuplicateReport.keep_source`)."""

    def __init__(self, bits: bytes | bytearray, offset: int, count: int, gen: int | None = None) -> None:
        super().__init__(bits, offset, count, keep=True, gen=gen)


# ----------------------------------------------------------------------------------
# several documents at once
# ----------------------------------------------------------------------------------
@dataclass
class MultiDuplicateReport:
    """Result of :func:`find_duplicates_many` (positions are GLOBAL: input ``i`` holds the
    positions ``offsets[i] .. offsets[i] + counts[i] - 1``).

    ``groups`` / ``duplicates`` (records marked: every member of a group but the kept one;
    ``keep="none"``: every member); ``per_input``: duplicates of each input; ``bits``: the
    marked global positions; ``examples``: ``(key text, [(input, position), ...])`` of the
    :data:`EXAMPLE_GROUPS` groups whose first record comes first (global order), with
    ``example_sizes``; ``records_with_key`` / ``missing`` / ``skipped`` / ``scanned`` summed
    over the inputs; ``names`` (file names), ``gens`` (state gen of each pinned input), ``key``,
    ``keep``, ``runs``, ``elapsed``, ``warnings`` / ``warning_params`` (texts:
    :meth:`warning_texts`)."""

    groups: int
    duplicates: int
    per_input: list[int]
    offsets: list[int]
    counts: list[int]
    bits: bytearray
    examples: list[tuple[str, list[tuple[int, int]]]] = field(default_factory=list)
    example_sizes: list[int] = field(default_factory=list)
    records_with_key: int = 0
    missing: int = 0
    skipped: int = 0
    scanned: int = 0
    key: str = ""
    keep: str = "first"
    names: list[str] = field(default_factory=list)
    gens: list[int | None] = field(default_factory=list)
    runs: int = 0
    elapsed: float = 0.0
    warnings: tuple[str, ...] = ()
    warning_params: tuple[dict[str, Any], ...] = ()

    @property
    def total(self) -> int:
        """Records of all inputs."""
        return sum(self.counts)

    def locate(self, position: int) -> tuple[int, int]:
        """``(input, local position)`` of a global position."""
        if not 0 <= position < self.total:
            raise IndexError(f"position {position} out of range ({self.total} records)")
        return _locate(self.offsets, self.counts, position)

    def is_duplicate(self, i: int, position: int) -> bool:
        """Record ``position`` of input ``i`` is marked."""
        if not 0 <= position < self.counts[i]:
            raise IndexError(f"record {position} out of range ({self.counts[i]} records)")
        g = self.offsets[i] + position
        return bool(self.bits[g >> 3] >> (g & 7) & 1)

    def keep_source(self, i: int) -> KeepSource:
        """The ascending positions of input ``i`` that are NOT marked (what a merge writes)."""
        return KeepSource(self.bits, self.offsets[i], self.counts[i], self.gens[i] if self.gens else None)

    def duplicate_source(self, i: int) -> PositionSource:
        """The ascending positions of input ``i`` that ARE marked."""
        return PositionSource(self.bits, self.offsets[i], self.counts[i], keep=False,
                              gen=self.gens[i] if self.gens else None)

    def warning_texts(self) -> list[str]:
        """The warnings in the current UI language."""
        return _warning_texts(self.warnings, self.warning_params)


def _locate(offsets: list[int], counts: list[int], g: int) -> tuple[int, int]:
    """``(input, local position)`` of global position ``g`` (empty inputs are skipped)."""
    i = bisect.bisect_right(offsets, g) - 1
    while i > 0 and g - offsets[i] >= counts[i]:   # pragma: no cover - only past the end
        i -= 1
    return i, g - offsets[i]


def find_duplicates_many(docs: Sequence[Any], key: str, *, keep: str = "first", memory_limit: int | None = None,
                         progress: Progress | None = None, cancel: CancelToken | None = None, strategy: str = "auto",
                         block_bytes: int = 8 * MiB) -> MultiDuplicateReport:
    """Duplicates by ``key`` across all records of several documents (module docstring
    "Stage-5 extensions"): one key pass per input into ONE set of entries (shared spilled
    runs, global positions), one merge, one bitmap. ``keep``: ``"first"`` (the earliest input,
    then the smallest position), ``"last"`` or ``"none"``; ``key`` as for
    :func:`find_duplicates` (``$`` = the whole record, ``$key`` = the member key). Every input
    is pinned and checked with ``ensure_unchanged`` after its scan. Raises like
    :func:`find_duplicates`; ``ValueError`` for an empty ``docs``."""
    started = time.perf_counter()
    if not docs:
        raise ValueError("find_duplicates_many needs at least one document")
    pinned = [d.pinned() for d in docs]
    key_text, path = _parse_key(key)
    keep = _check_keep(keep, allow_none=True)
    strategy = _check_strategy(strategy)
    settings = getattr(pinned[0], "settings", None) or get_settings()
    limit = _memory_limit(memory_limit, settings)
    names = [d.source_path.name for d in pinned]
    # -- indexes -----------------------------------------------------------------------------
    todo = [d for d in pinned if not d.index_complete]
    bar = _Bar(progress, sum(max(1, int(d.data_size)) for d in pinned),
               t("dupes.progress.scan_many", count=len(pinned)))
    if todo:
        index_p = bar.index()
        for j, d in enumerate(todo):
            sub = index_p.child(j / len(todo), (j + 1) / len(todo), total=max(1, int(d.data_size))) \
                if index_p is not None else None
            d.build_index(progress=sub, cancel=cancel)
    check(cancel)
    sizes = [_data_bytes(d) for d in pinned]
    counts = [int(d.record_count) for d in pinned]
    offsets: list[int] = []
    total = 0
    for n in counts:
        offsets.append(total)
        total += n
    runs = HashRuns(work_dir(settings.cache_dir), cancel)
    max_entries = max(MIN_RUN_ENTRIES, limit // ENTRY_BYTES)
    hashers: list[KeyHasher] = []
    entries: list[int] = []
    try:
        # -- pass 1: the keys of every input ---------------------------------------------------
        whole = max(1, sum(sizes))
        scan_p = bar.scan(whole)
        done = 0
        for i, d in enumerate(pinned):
            h = KeyHasher(d, path, strategy, settings, cancel, max_entries, runs, None, offset=offsets[i],
                          entries=entries)
            hashers.append(h)
            sub = scan_p.child(done / whole, (done + sizes[i]) / whole, total=sizes[i]) \
                if scan_p is not None else None
            if path is None and not h.member:
                h.missing += counts[i]          # $key without member records: nothing to read
                if sub is not None:
                    sub.finish()
            else:
                _scan_blocks(d, None, h, block_bytes, sub, cancel)
            entries = h.entries
            done += sizes[i]
            check(cancel)
            _ensure_unchanged(d)
        if scan_p is not None:
            scan_p.finish()
        # -- pass 2: one merge over all inputs -------------------------------------------------
        n_entries = sum(h.with_key for h in hashers)
        merge_p = bar.merge(n_entries, t("dupes.progress.merge_many", count=len(pinned)))
        bits = bytearray((total + 7) >> 3)
        for h in hashers:
            h.entries = []
        stream = runs.merged(entries)
        entries = []
        try:
            groups, dups, examples, _ = _walk_groups(stream, keep, bits, cancel, merge_p)
        finally:
            stream.close()
        if merge_p is not None:
            merge_p.finish()
        # -- the examples' key texts, read from their inputs ------------------------------------
        firsts: dict[int, list[int]] = {}
        for first, _positions, _size in examples:
            i, p = _locate(offsets, counts, first)
            firsts.setdefault(i, []).append(p)
        texts: dict[tuple[int, int], str] = {}
        for i, local in firsts.items():
            for p, text in _example_texts(pinned[i], hashers[i], local, cancel).items():
                texts[(i, p)] = text
    finally:
        runs.cleanup()
        _release(entries)
    bar.finish()
    # -- the report ----------------------------------------------------------------------------
    with_key = sum(h.with_key for h in hashers)
    missing = sum(h.missing for h in hashers)
    skipped = sum(h.too_large + h.unreadable for h in hashers)
    scanned = with_key + missing + skipped
    warnings: list[str] = []
    params: list[dict[str, Any]] = []
    for i, h in enumerate(hashers):
        if path is None and not h.member and counts[i]:
            warnings.append("dupes.warn.input_no_record_keys")
            params.append({"name": names[i], "count": counts[i]})
    if path is not None and scanned and with_key == 0 and missing == scanned:
        warnings.append("dupes.warn.key_absent")
        params.append({"key": key_text, "count": scanned})
    for i, h in enumerate(hashers):
        if h.too_large:
            warnings.append("dupes.warn.input_too_large")
            params.append({"name": names[i], "count": h.too_large, "record": h.first_too_large,
                           "size": format_bytes(h.budget)})
        if h.unreadable:
            warnings.append("dupes.warn.input_unreadable")
            params.append({"name": names[i], "count": h.unreadable, "record": h.first_unreadable})
    per_input = [_bits_range(bits, offsets[i], counts[i]).bit_count() for i in range(len(pinned))]
    if sum(per_input) != dups:  # pragma: no cover - would be a bug
        raise RuntimeError(f"duplicate finder: {sum(per_input)} marked records for {dups} duplicates")
    elapsed = time.perf_counter() - started
    report = MultiDuplicateReport(
        groups=groups, duplicates=dups, per_input=per_input, offsets=offsets, counts=counts, bits=bits,
        examples=[(texts.get(_locate(offsets, counts, first), ""), [_locate(offsets, counts, g) for g in positions])
                  for first, positions, _ in examples],
        example_sizes=[size for _, _, size in examples], records_with_key=with_key, missing=missing,
        skipped=skipped, scanned=scanned, key=key_text, keep=keep, names=names,
        gens=[getattr(d, "gen", None) for d in pinned], runs=runs.written, elapsed=elapsed,
        warnings=tuple(warnings), warning_params=tuple(params))
    log.info("duplicates of %d inputs (%s) by %s: %d groups, %d duplicates %s, %d with the key, %d missing, "
             "%d skipped (%.2f s, %d runs)", len(pinned), ", ".join(names[:5]), key_text, groups, dups,
             per_input[:10], with_key, missing, skipped, elapsed, runs.written)
    return report
