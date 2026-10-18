r"""Structure inference ("schema") from a sample of records (AGENT_PROMPT §4.4, §9, §13).

:func:`infer_schema` analyses an evenly spread, deterministic sample of a document's records
(or every record) and merges them into one tree of :class:`FieldInfo` nodes -- one node per
*path pattern*, all array elements merged as ``[*]`` -- with per-field statistics:
occurrences, records containing the field, presence, type distribution, example values,
string length, number range, array length and object key-count ranges. The result is a
:class:`Schema`: pre-order :meth:`Schema.fields`, :meth:`Schema.find`, JSON-serializable
:meth:`Schema.to_dict` / :meth:`Schema.from_dict`, and :meth:`Schema.format_table` (the
``jsonreshape inspect`` table). :class:`SchemaBuilder` is the accumulator behind it (also
usable directly on in-memory values or ijson events).

Sampling
========
* The *population* is ``source``: ``None`` = every record of the document (the index is
  built first when needed, because the record count is required), a ``range``, a sequence
  with ``__len__``/``__getitem__`` (list, ``array``, RecordSet) or any other iterable of
  record indices (consumed once).
* ``sample=None``, or a population of at most ``sample`` records: every record is analysed.
  Otherwise the records at positions ``j * (N - 1) // (sample - 1)`` for
  ``j = 0 .. sample - 1`` (:func:`sample_positions`): evenly spread, deterministic, the first
  and the last record included (``sample=1``: the first record). An iterable without
  ``len()`` is thinned on the fly (keep every ``stride``-th index, doubling ``stride``
  whenever more than ``2 * sample + 1`` are kept), then the same positions are taken from
  what was kept (rounded down to the kept grid; the last index is always kept), so at most
  ``2 * sample + 2`` indices are ever held.
* Records are analysed in population order (the order of a sorted RecordSet, file order
  for the base set). They are read with :meth:`Document.iter_blocks`: a full scan in
  8 MiB blocks, a sample in small blocks so that the bytes between far-apart sampled
  records are never read (``SAMPLE_BLOCK_BYTES``; the reader coalesces records closer than
  32 KiB into one read). A sample of ``PREFETCH_MIN_RECORDS`` (1024) records or more is
  read ahead, in order, by a shared pool of ``PREFETCH_THREADS`` (4) threads while the
  calling thread analyses: when the file is not in the OS cache every sampled record costs
  a random read (~0.1 ms on NVMe), and parallel requests cut 10000 such reads from
  ~1.2 s to ~0.14 s with 8 threads (measured with unbuffered reads). 4 threads keep most
  of that gain while competing less for the GIL with the GUI thread. At most
  ~``PREFETCH_WINDOW_BYTES`` (64 MiB, estimated from the average record size) are read
  ahead.

Statistics of a field (one path pattern)
========================================
* ``count``: occurrences (values at the path); ``records``: records holding at least one.
* **Presence** = ``present / base``. The *context* of a field is its nearest array-element
  ancestor (a ``[*]`` node, the field itself excluded), or the record when there is none.
  ``base`` = the number of contexts: records scanned for fields outside arrays, the
  occurrences of the enclosing ``[*]`` node for fields under arrays. ``present`` = the
  contexts in which the field occurs at least once. For object keys this is ``records``
  (outside arrays) or ``count`` (under arrays: one value per element under dict
  semantics); for a ``[*]`` node it counts the contexts holding a non-empty array:
  ``tags[*]`` = records with at least one tag, ``orders[*].items[*]`` = orders with at
  least one item. ``null`` counts as present (the key exists); ``missing = base -
  present``. Example: ``orders[*].sum`` present in 3 of 4 order elements -> 75 %, whatever
  the number of records.
* ``types``: occurrences per JSON type (``object array string number bool null``, the
  names of :func:`jsonreshape.codec.json_type`), most frequent first (ties in that order).
* ``examples``: the first ``MAX_EXAMPLES`` (5) distinct example texts in analysis order
  (records in order, each walked in document order), scalars only: JSON text -- strings
  quoted and escaped like ``json.dumps(ensure_ascii=False)``, exact number text,
  ``true``/``false``/``null`` -- of at most ``EXAMPLE_CHARS`` (60) characters (longer texts
  are cut and end with ``…``; a cut string has no closing quote). Characters that would
  disturb a terminal or reorder text (C1 controls, DEL, line/paragraph separators, bidi
  controls, BOM, lone surrogates) are written as ``\uXXXX`` (:func:`example_text`).
* ``str_len``: (min, max) string length in characters; ``num_range``: (min, max) exact
  number *texts*. Numbers are compared exactly by their decimal value (``int`` as is,
  plain floats as their decimal token, :class:`~jsonreshape.codec.JsonFloat` as
  ``Decimal(raw)``), so ``1e400`` is above every double and ``0.10000000000000000001`` is
  above ``0.1``; between equal values the first seen text is kept (``1.10`` vs ``1.1``).
  ``arr_len``: (min, max) array length; ``obj_keys``: (min, max) distinct keys. ``None``
  when the field never held that type.
* Member records (keyed documents; wrapped documents whose record container is an object):
  the record *value* is analysed at the root (``$``) and the member key as the pseudo-field
  ``$key`` -- text ``$key`` (the filter-language name), path ``("$key",)``,
  ``is_key=True``, the first child of the root, stored in ``root.children`` under the
  sentinel :data:`RECORD_KEY`, so that a real member named ``"$key"`` (text ``["$key"]``)
  stays a separate field. Consumers that turn paths into patterns must check ``is_key``.
* Children are ordered by first appearance.

Limits (bounded memory)
=======================
* An object field keeps at most ``MAX_KEYS`` (1000) distinct child keys; further new keys
  (e.g. maps keyed by ids or dates) are pooled into the wildcard child ``*`` (path segment
  :attr:`~jsonreshape.paths.Wild.ANY_KEY`, text ``scores.*``).
* At most ``MAX_FIELDS`` (20000) fields per schema and ``MAX_DEPTH`` (128) levels: values
  that would need a field beyond these limits are counted at their container (type,
  length) but not walked, and :attr:`Schema.truncated` is set.

Big records
===========
``codec.loads`` of a big record needs about 25 times its size in Python objects (a 45 MB
single document: +1.1 GB). Records larger than :data:`STREAM_RECORD_BYTES` (1 MiB) --
``huge`` blocks (``Settings.large_node_bytes``) included -- and records the JSON decoders
refuse although the index accepted them (nesting deeper than they support) are therefore
*walked* into the builder instead of parsed whole, with exactly the statistics the parsed
record would give (same values, same order, same limits; tested by a property test):

* every ``codec.loads`` input stays at most ``STREAM_RECORD_BYTES`` and below the
  huge-record size ``Settings.large_node_bytes`` (a huge record is never parsed whole,
  nor a value as big): a value within that limit is parsed and added as usual; a bigger
  object / array is fed to the Rust structure scanner (``core.Scanner``,
  ``MODE_CONTAINER``, :data:`FEED_BYTES` at a time) that reports the spans of its direct
  children: consecutive small children are parsed together (at most
  :data:`PARSE_BATCH_BYTES` per ``codec.loads``), big ones are walked the same way, one
  level deeper. A bigger string is decoded piece by piece (its statistics need only its
  length in characters and its first characters; a piece never ends inside an escape or a
  UTF-8 character, nor between the two escapes of a surrogate pair); a bigger number
  token (rare) is parsed alone. Scalars therefore come from ``codec.loads`` itself:
  exact number texts, ``-0``, lone surrogate escapes, keys -- no ijson deviation.
* Duplicate keys get dict semantics (the last value, at the position of the first key):
  within a batch ``codec.loads`` applies them; across a big object's batches and big
  members the key hashes are remembered (:class:`_KeyHashes`: a set, beyond
  :data:`KEYSET_MAX` keys an ``array('Q')`` table of 16-32 bytes per key). A repeated hash
  (rare) undoes the record and walks it again with a plan per big object: one more pass
  over its keys finds the repeated ones, their first position and last value.
* All or nothing: while such a record is added, :class:`SchemaBuilder` logs the state of
  every field before the record first changes it (and the fields it creates), so a record
  that cannot be analysed after all (bytes that are not the JSON the index accepted) is
  undone completely: it is not counted in ``scanned`` but in ``skipped`` and logged;
  nothing of it enters the statistics.

Memory besides the builder: the parse batches (~25x :data:`PARSE_BATCH_BYTES`), up to
``STREAM_RECORD_BYTES`` + :data:`FEED_BYTES` of read bytes and the key hashes per open big
object; the bytes of a record inside an ordinary block are used where they are, a huge
record is read from the file.

The analysis runs in the calling thread (the GUI uses a
:class:`~jsonreshape.tasks.BackgroundTask`); progress is reported in bytes (index build when
needed, then the bytes of the analysed records: the bytes read for a pass over the whole
document, else the records' own bytes -- the reader also reads the small gaps between
sampled records -- with the total re-estimated when the records turn out bigger than
average, so ``done`` never exceeds ``total``), cancellation is checked at least every 256
records and, in a walked big record, every scanner chunk and parse batch. When the
analysis has finished, ``doc.ensure_unchanged()``
makes sure the file was not changed by another program meanwhile
(:class:`~jsonreshape.errors.FileChangedError`).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from array import array
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from decimal import Decimal
from itertools import islice
from typing import Any, Iterable, Iterator

from . import codec, core
from .codec import JsonFloat, JsonInt
from .core import MODE_CONTAINER
from .errors import JsonReshapeError, UserInputError
from .fileio import open_shared, read_range
from .i18n import get_language, t
from .paths import Wild, canonical_index, format_path, parse_pattern
from .reader import BLOCK_BYTES, Block, Document, split_member
from .tasks import CancelToken, Progress, check

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SAMPLE",
    "EXAMPLE_CHARS",
    "KEY_TEXT",
    "MAX_DEPTH",
    "MAX_EXAMPLES",
    "MAX_FIELDS",
    "MAX_KEYS",
    "RECORD_KEY",
    "ROOT_TEXT",
    "TYPE_NAMES",
    "FieldInfo",
    "Schema",
    "STREAM_RECORD_BYTES",
    "SchemaBuilder",
    "example_text",
    "infer_schema",
    "sample_positions",
]

DEFAULT_SAMPLE = 10000
MAX_EXAMPLES = 5
EXAMPLE_CHARS = 60
MAX_KEYS = 1000
MAX_FIELDS = 20000
MAX_DEPTH = 128
TYPE_NAMES = ("object", "array", "string", "number", "bool", "null")
ROOT_TEXT = "$"
KEY_TEXT = "$key"
SCHEMA_VERSION = 1
SAMPLE_BLOCK_BYTES = 32 * 1024     # sampled records: a block never spans more than this
PREFETCH_MIN_RECORDS = 1024        # smaller samples are read by the calling thread only
PREFETCH_THREADS = 4               # read-ahead threads for big samples (shared, created on demand)
STREAM_RECORD_BYTES = 1 << 20      # larger records (and values) are walked instead of parsed (memory)
PARSE_BATCH_BYTES = 32 << 10       # small children of a walked container: parsed this many bytes at a time
FEED_BYTES = 256 << 10             # bytes of a walked container given to the structure scanner at once
KEYSET_MAX = 1 << 12               # key hashes of a walked object kept in a set, beyond: a compact table
_KEY_PROBE = 4 << 10               # bytes read to decode the key of a big member
PREFETCH_CHUNK_RECORDS = 128       # records per read-ahead task (fewer for big records) ...
PREFETCH_CHUNK_BYTES = 2 << 20     # ... about this many bytes of records per task
PREFETCH_WINDOW_BYTES = 64 << 20   # estimated record bytes read ahead of the analysis
CHECK_RECORDS = 256                # cancel check interval (records)
CHECK_EVENTS = 4096                # cancel check interval (streamed events)
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"

_EXK_CHARS = EXAMPLE_CHARS + 4     # string prefix that determines an example text
_EXK_MAX = 256                     # strings remembered per field while looking for examples
_INT_MIN = -(2**63)
_UINT_MAX = 2**64 - 1
_ANY = Wild.ANY_INDEX
_ANY_KEY = Wild.ANY_KEY

# characters written as \uXXXX in example/table text (besides JSON's own escapes): C0 (only
# reachable through unquoted keys in path texts), DEL and C1 controls, ALM/LRM/RLM,
# line/paragraph separators and bidi embeddings, bidi isolates, BOM, lone surrogates
# (built with chr() so this source stays plain ASCII)
_DISPLAY_ESCAPE = "".join((
    "\x00-\x1f\x7f-\x9f", chr(0x061C), chr(0x200E), chr(0x200F), chr(0x2028), "-", chr(0x202E),
    chr(0x2066), "-", chr(0x2069), chr(0xFEFF), chr(0xD800), "-", chr(0xDFFF),
))
_DISPLAY_RE = re.compile(f"[{_DISPLAY_ESCAPE}]")
_JSON_ESCAPES = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}


class _RecordKey:
    """Type of :data:`RECORD_KEY` (a unique, hashable sentinel)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "RECORD_KEY"

    def __reduce__(self) -> str:
        return "RECORD_KEY"


RECORD_KEY: Any = _RecordKey()
"""Key of the ``$key`` pseudo-field in ``root.children`` (member records)."""


# ----------------------------------------------------------------------------------
# example texts
# ----------------------------------------------------------------------------------
def _escape_display(text: str) -> str:
    return _DISPLAY_RE.sub(lambda m: "\\u%04x" % ord(m.group()), text)


def _json_char(ch: str) -> str:
    esc = _JSON_ESCAPES.get(ch)
    if esc is not None:
        return esc
    if ch < " " or _DISPLAY_RE.match(ch):
        return "\\u%04x" % ord(ch)
    return ch


def _string_example(s: str, limit: int) -> str:
    if len(s) <= limit:
        text = _escape_display(json.dumps(s, ensure_ascii=False))
        if len(text) <= limit:
            return text
    out = ['"']
    used = 1
    for ch in s:
        piece = _json_char(ch)
        if used + len(piece) > limit - 1:
            break
        out.append(piece)
        used += len(piece)
    out.append(ELLIPSIS)
    return "".join(out)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + ELLIPSIS


def example_text(value: Any, limit: int = EXAMPLE_CHARS) -> str | None:
    """Display text of a scalar as used in :attr:`FieldInfo.examples` (see the module
    docstring); ``None`` for containers."""
    limit = max(2, int(limit))
    if isinstance(value, str):
        return _string_example(value, limit)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float, Decimal)):
        return _cut(codec.number_text(value), limit)
    return None


# ----------------------------------------------------------------------------------
# exact number comparison
# ----------------------------------------------------------------------------------
def _num_key(v: Any) -> Any:
    """Exactly comparable key of a number value (``None`` for NaN)."""
    tp = type(v)
    if tp is int or tp is float:
        return v if v == v else None
    if tp is JsonFloat:
        return Decimal(v.raw)
    if tp is JsonInt:
        return int(v)
    if isinstance(v, Decimal):
        return None if v.is_nan() else v
    if isinstance(v, int):
        return int(v)
    f = float(v)
    return f if f == f else None


def _num_lt(a: Any, b: Any) -> bool:
    """``a < b`` by exact decimal value; plain floats compare as their (shortest
    round-trip) decimal text when the other side is a Decimal."""
    if type(a) is float:
        if type(b) is Decimal:
            return Decimal(repr(a)) < b
    elif type(b) is float and type(a) is Decimal:
        return a < Decimal(repr(b))
    return a < b


# ----------------------------------------------------------------------------------
# accumulator node
# ----------------------------------------------------------------------------------
class _Acc:
    """Mutable statistics of one path pattern while records are added."""

    __slots__ = ("path", "text", "depth", "elem", "is_key", "children", "count", "records", "present",
                 "rec", "unit", "tc", "slo", "shi", "alo", "ahi", "klo", "khi", "nlo", "nhi", "nlo_v", "nhi_v",
                 "examples", "exfull", "exk")

    def __init__(self, path: tuple, text: str, depth: int, *, elem: bool = False, is_key: bool = False) -> None:
        self.path = path
        self.text = text
        self.depth = depth
        self.elem = elem              # an array-element node ([*]): a presence context
        self.is_key = is_key
        self.children: dict[Any, _Acc] = {}
        self.count = 0
        self.records = 0
        self.present = 0
        self.rec = -1                 # serial of the last record that touched this field
        self.unit = -1                # last context unit that touched this field
        self.tc = [0, 0, 0, 0, 0, 0]  # occurrences per TYPE_NAMES
        self.slo = 1 << 62
        self.shi = -1
        self.alo = 1 << 62
        self.ahi = -1
        self.klo = 1 << 62
        self.khi = -1
        self.nlo: Any = None          # exact comparison keys of the number range
        self.nhi: Any = None
        self.nlo_v: Any = None        # the values themselves (for their exact text)
        self.nhi_v: Any = None
        self.examples: list[str] = []
        self.exfull = False
        self.exk: set[str] | None = None


_NEW_FIELD: Any = object()   # SchemaBuilder journal entry: (_NEW_FIELD, parent, segment)


def _add_example(f: _Acc, text: str) -> None:
    ex = f.examples
    if text not in ex:
        ex.append(text)
        if len(ex) >= MAX_EXAMPLES:
            f.exfull = True
            f.exk = None


def _add_str_example(f: _Acc, s: str) -> None:
    key = s if len(s) <= _EXK_CHARS else s[:_EXK_CHARS]
    exk = f.exk
    if exk is None:
        exk = f.exk = set()
    elif key in exk:
        return
    if len(exk) < _EXK_MAX:
        exk.add(key)
    _add_example(f, _string_example(s, EXAMPLE_CHARS))


def _number(f: _Acc, key: Any, v: Any) -> None:
    lo = f.nlo
    if lo is None:
        f.nlo = f.nhi = key
        f.nlo_v = f.nhi_v = v
    elif _num_lt(key, lo):
        f.nlo = key
        f.nlo_v = v
    elif _num_lt(f.nhi, key):
        f.nhi = key
        f.nhi_v = v


def _event_number(v: Any) -> Any:
    """ijson number -> the type ``codec.loads`` gives the same token (cf. stream.py)."""
    if type(v) is int:
        return v if _INT_MIN <= v <= _UINT_MAX else codec.loads(str(v))
    return codec.loads(str(v))


class SchemaBuilder:
    """Accumulates the structure of records one by one; :meth:`build` returns a
    :class:`Schema` snapshot (independent of later additions).

    ``max_fields`` / ``max_keys`` / ``max_depth``: the limits of the module docstring.
    """

    def __init__(self, *, max_fields: int = MAX_FIELDS, max_keys: int = MAX_KEYS, max_depth: int = MAX_DEPTH) -> None:
        if max_fields < 1 or max_keys < 1 or max_depth < 1:
            raise ValueError("schema limits must be >= 1")
        self.max_fields = int(max_fields)
        self.max_keys = int(max_keys)
        self.max_depth = int(max_depth)
        self.root = _Acc((), ROOT_TEXT, 0)
        self.scanned = 0
        self.truncated = False
        self._nfields = 0
        self._rec = 0
        self._unit = 0
        # undo log of the record being added (streamed records: all or nothing), else None
        self._journal: list[tuple[Any, ...]] | None = None
        self._saved: tuple[int, bool, int] = (0, False, 0)

    # -- nodes ---------------------------------------------------------------------------
    def _child(self, f: _Acc, seg: Any) -> _Acc | None:
        """The child of ``f`` for path segment ``seg`` (created; ``None`` when a limit
        prevents it). New keys beyond ``max_keys`` are pooled into ``*``."""
        ch = f.children
        if seg.__class__ is str and len(ch) >= self.max_keys:
            pool = ch.get(_ANY_KEY)
            if pool is not None:
                return pool
            seg = _ANY_KEY
        if self._nfields >= self.max_fields:
            self.truncated = True
            return None
        if seg is RECORD_KEY:
            c = _Acc((KEY_TEXT,), KEY_TEXT, 1, is_key=True)
        else:
            path = f.path + (seg,)
            c = _Acc(path, format_path(path), f.depth + 1, elem=seg is _ANY)
        ch[seg] = c
        self._nfields += 1
        if self._journal is not None:
            self._journal.append((_NEW_FIELD, f, seg))
        return c

    # -- all-or-nothing additions (streamed records) -------------------------------------
    def _begin(self) -> None:
        """Start logging the changes of the next record, so that :meth:`_rollback` can
        take it back completely."""
        self._journal = []
        self._saved = (self.scanned, self.truncated, self._nfields)

    def _commit(self) -> None:
        self._journal = None

    def _rollback(self) -> None:
        """Undo every change since :meth:`_begin` (fields created by the record removed,
        the statistics of the others restored)."""
        journal = self._journal
        self._journal = None
        if journal is None:
            return
        for entry in reversed(journal):
            f = entry[0]
            if f is _NEW_FIELD:
                del entry[1].children[entry[2]]
                continue
            (_, f.count, f.records, f.present, f.rec, f.unit, f.tc, f.slo, f.shi, f.alo, f.ahi, f.klo, f.khi,
             f.nlo, f.nhi, f.nlo_v, f.nhi_v, nex, f.exfull, f.exk) = entry
            del f.examples[nex:]
        self.scanned, self.truncated, self._nfields = self._saved

    def _snap(self, f: _Acc) -> None:
        """Log the state of ``f`` before the current record changes it (once per record:
        called where ``f.rec`` is first set to the record's serial)."""
        exk = f.exk
        assert self._journal is not None
        self._journal.append((f, f.count, f.records, f.present, f.rec, f.unit, f.tc[:], f.slo, f.shi, f.alo, f.ahi,
                              f.klo, f.khi, f.nlo, f.nhi, f.nlo_v, f.nhi_v, len(f.examples), f.exfull,
                              None if exk is None else set(exk)))

    def _enter(self, f: _Acc, own: int) -> None:
        """Count one value at ``f`` (occurrence, record, context); its type is counted by
        the caller."""
        if f.rec != self._rec:
            if self._journal is not None:
                self._snap(f)
            f.rec = self._rec
            f.records += 1
        f.count += 1
        if f.unit != own:
            f.unit = own
            f.present += 1

    def _get(self, f: _Acc, seg: Any) -> _Acc | None:
        c = f.children.get(seg)
        return c if c is not None else self._child(f, seg)

    # -- in-memory values ------------------------------------------------------------
    def add(self, value: Any, key: str | None = None) -> None:
        """Add one record: its (parsed) ``value`` and, for member records, its ``key``."""
        self._rec += 1
        self._unit += 1
        unit = self._unit
        if key is not None:
            self._add_key(key, unit)
        self._visit(self.root, value, unit, unit)
        self.scanned += 1

    def _add_key(self, key: str, unit: int) -> None:
        f = self._get(self.root, RECORD_KEY)
        if f is not None:
            self._visit(f, str(key), unit, unit)

    def _visit(self, f: _Acc, v: Any, own: int, kids: int) -> None:
        """Count value ``v`` at field ``f``; ``own`` = its context unit, ``kids`` = the
        context unit of its children (a fresh one for array elements)."""
        if f.rec != self._rec:
            if self._journal is not None:
                self._snap(f)
            f.rec = self._rec
            f.records += 1
        f.count += 1
        if f.unit != own:
            f.unit = own
            f.present += 1
        tp = type(v)
        if tp is str:
            f.tc[2] += 1
            n = len(v)
            if n < f.slo:
                f.slo = n
            if n > f.shi:
                f.shi = n
            if not f.exfull:
                _add_str_example(f, v)
        elif tp is dict:
            self._dict(f, v, kids)
        elif tp is int:
            f.tc[3] += 1
            lo = f.nlo
            if lo is None:
                f.nlo = f.nhi = f.nlo_v = f.nhi_v = v
            elif v < lo:
                f.nlo = f.nlo_v = v
            elif v > f.nhi:
                f.nhi = f.nhi_v = v
            if not f.exfull:
                _add_example(f, int.__repr__(v))
        elif tp is float:
            f.tc[3] += 1
            if v == v:
                _number(f, v, v)
            if not f.exfull:
                _add_example(f, codec.number_text(v))
        elif tp is list:
            self._list(f, v, kids)
        elif tp is bool:
            f.tc[4] += 1
            if not f.exfull:
                _add_example(f, "true" if v else "false")
        elif v is None:
            f.tc[5] += 1
            if not f.exfull:
                _add_example(f, "null")
        else:
            self._other(f, v, kids)

    def _dict(self, f: _Acc, v: dict, kids: int) -> None:
        f.tc[0] += 1
        n = len(v)
        if n < f.klo:
            f.klo = n
        if n > f.khi:
            f.khi = n
        if not n:
            return
        if f.depth >= self.max_depth:
            self.truncated = True
            return
        ch = f.children
        visit = self._visit
        for k, x in v.items():
            if k.__class__ is not str:
                k = str(k)
            c = ch.get(k)
            if c is None:
                c = self._child(f, k)
                if c is None:
                    continue
            visit(c, x, kids, kids)

    def _list(self, f: _Acc, v: Any, kids: int) -> None:
        f.tc[1] += 1
        n = len(v)
        if n < f.alo:
            f.alo = n
        if n > f.ahi:
            f.ahi = n
        if not n:
            return
        if f.depth >= self.max_depth:
            self.truncated = True
            return
        c = f.children.get(_ANY)
        if c is None:
            c = self._child(f, _ANY)
            if c is None:
                return
        visit = self._visit
        for x in v:
            self._unit += 1
            visit(c, x, kids, self._unit)

    def _other(self, f: _Acc, v: Any, kids: int) -> None:
        """Values of other (sub)types: JsonFloat/JsonInt, Decimal, int/float/str/dict
        subclasses, tuples."""
        if isinstance(v, bool):  # pragma: no cover - bool cannot be subclassed
            v = bool(v)
        if isinstance(v, (int, float, Decimal)):
            f.tc[3] += 1
            key = _num_key(v)
            if key is not None:
                _number(f, key, v)
            if not f.exfull:
                _add_example(f, _cut(codec.number_text(v), EXAMPLE_CHARS))
        elif isinstance(v, str):
            s = str(v)
            f.tc[2] += 1
            n = len(s)
            f.slo = min(f.slo, n)
            f.shi = max(f.shi, n)
            if not f.exfull:
                _add_str_example(f, s)
        elif isinstance(v, dict):
            self._dict(f, v, kids)
        elif isinstance(v, (list, tuple)):
            self._list(f, v, kids)
        else:
            raise TypeError(f"not a JSON value: {type(v).__name__}")

    # -- streamed records (ijson events) ---------------------------------------------
    def add_events(self, events: Iterable[tuple[str, Any]], *, member: bool = False,
                   cancel: CancelToken | None = None) -> None:
        """Add one record given as ``ijson.basic_parse`` events (numbers as ``int`` /
        ``Decimal``), e.g. from :func:`jsonreshape.stream.iter_events`. ``member``: the
        events are those of ``{"key": value}`` (a member record). The ijson deviations of
        :mod:`jsonreshape.stream` apply (number texts, lone surrogates) and duplicate keys
        are counted per occurrence. When the events raise, the builder holds a partial
        record. (:func:`infer_schema` does not use events: big records are analysed
        exactly like parsed ones, see the module docstring.)"""
        self._rec += 1
        self._unit += 1
        rec_unit = self._unit
        it = iter(events)
        if member:
            ev, _ = next(it)
            if ev != "start_map":
                raise ValueError("a member record must start with start_map")
            ev, key = next(it)
            if ev != "map_key":
                raise ValueError("a member record must start with a key")
            self._add_key(key, rec_unit)
        visit = self._visit
        # frames: [field or None (not analysed), is_map, kids unit, current key, children seen, walk children]
        stack: list[list[Any]] = []
        n = 0
        for ev, val in it:
            n += 1
            if n & (CHECK_EVENTS - 1) == 0:
                check(cancel)
            if ev == "map_key":
                top = stack[-1]
                top[3] = val
                top[4] += 1
                continue
            if ev == "end_map" or ev == "end_array":
                if not stack:
                    break  # the closing brace of a member record's wrapper
                fr = stack.pop()
                f = fr[0]
                if f is not None:
                    cnt = fr[4]
                    if fr[1]:
                        if cnt < f.klo:
                            f.klo = cnt
                        if cnt > f.khi:
                            f.khi = cnt
                    else:
                        if cnt < f.alo:
                            f.alo = cnt
                        if cnt > f.ahi:
                            f.ahi = cnt
                continue
            # a value starts
            if not stack:
                f: _Acc | None = self.root
                own = kids = rec_unit
            else:
                top = stack[-1]
                if not top[1]:
                    top[4] += 1
                if not top[5]:
                    if top[0] is not None:
                        self.truncated = True   # a live container at the depth limit
                    f = None
                elif top[1]:
                    f = self._get(top[0], top[3] if top[3].__class__ is str else str(top[3]))
                    own = kids = top[2]
                else:
                    f = self._get(top[0], _ANY)
                    own = top[2]
                    self._unit += 1
                    kids = self._unit
            if ev == "start_map" or ev == "start_array":
                is_map = ev == "start_map"
                if f is None:
                    stack.append([None, is_map, 0, None, 0, False])
                    continue
                f.count += 1
                if f.rec != self._rec:
                    f.rec = self._rec
                    f.records += 1
                if f.unit != own:
                    f.unit = own
                    f.present += 1
                f.tc[0 if is_map else 1] += 1
                deep = f.depth >= self.max_depth
                stack.append([f, is_map, kids, None, 0, not deep])
                continue
            if f is None:
                continue
            if ev == "number":
                val = _event_number(val)
            visit(f, val, own, kids)
        if stack:
            raise ValueError("incomplete event stream")
        self.scanned += 1

    # -- merging -----------------------------------------------------------------------
    def merge(self, other: SchemaBuilder) -> None:
        """Add the records analysed by ``other`` (they come after this builder's records
        for the example order).

        Exact (equal to adding ``other``'s records here) unless a limit treats a key
        differently in the two builders: several fields of ``other`` that land in one
        field here (keys pooled into ``*`` only here) cannot tell in how many records or
        contexts they occurred *together*, so that field's ``records`` / ``present``
        increments are the sum, capped by its parent's (a field never occurs in more
        records or contexts than its parent): an upper bound, exact when ``other`` holds
        one record. :func:`infer_schema` never merges (big records are streamed into the
        builder itself)."""
        self.scanned += other.scanned
        self.truncated = self.truncated or other.truncated
        self._merge(self.root, [other.root], None, None)

    def _merge(self, dst: _Acc, srcs: list[_Acc], cap_records: int | None, cap_present: int | None) -> None:
        """Add the statistics of the nodes ``srcs`` (of another builder) that map to
        ``dst`` (several when their keys are pooled here only); ``cap_*``: the bounds of
        the ``records`` / ``present`` increments (``None`` for the root)."""
        count = records = present = 0
        for src in srcs:
            count += src.count
            records += src.records
            present += src.present
        if cap_records is not None and cap_present is not None:
            records = min(records, cap_records)
            present = min(present, cap_present)
        dst.count += count
        dst.records += records
        dst.present += present
        for src in srcs:
            for i in range(6):
                dst.tc[i] += src.tc[i]
            dst.slo = min(dst.slo, src.slo)
            dst.shi = max(dst.shi, src.shi)
            dst.alo = min(dst.alo, src.alo)
            dst.ahi = max(dst.ahi, src.ahi)
            dst.klo = min(dst.klo, src.klo)
            dst.khi = max(dst.khi, src.khi)
            if src.nlo is not None:
                if dst.nlo is None:
                    dst.nlo, dst.nlo_v, dst.nhi, dst.nhi_v = src.nlo, src.nlo_v, src.nhi, src.nhi_v
                else:
                    if _num_lt(src.nlo, dst.nlo):
                        dst.nlo, dst.nlo_v = src.nlo, src.nlo_v
                    if _num_lt(dst.nhi, src.nhi):
                        dst.nhi, dst.nhi_v = src.nhi, src.nhi_v
            for text in src.examples:
                if dst.exfull:
                    break
                _add_example(dst, text)
        # the children, grouped by the field they land in here (first-seen order)
        groups: dict[int, tuple[_Acc, list[_Acc]]] = {}
        for src in srcs:
            for seg, sc in src.children.items():
                dc = self._get(dst, seg)
                if dc is None:
                    continue
                group = groups.get(id(dc))
                if group is None:
                    groups[id(dc)] = (dc, [sc])
                else:
                    group[1].append(sc)
        # a child occurs in at most the records of its parent, and in at most the parent's
        # contexts (an element node: the parent's occurrences, each a context of its own)
        child_present = count if dst.elem else present
        for dc, scs in groups.values():
            self._merge(dc, scs, records, child_present)

    # -- result ------------------------------------------------------------------------
    def build(self, *, total_records: int | None = None, sample: int | None = None, complete: bool | None = None,
              elapsed: float = 0.0, skipped: int = 0) -> Schema:
        """Snapshot of the statistics as a :class:`Schema` (presence computed here).
        ``total_records`` defaults to the records added (+ ``skipped``); ``complete``
        defaults to "every record of the population was analysed"."""
        scanned = self.scanned
        total = scanned + skipped if total_records is None else int(total_records)
        if complete is None:
            complete = skipped == 0 and scanned >= total
        root = _finish(self.root, scanned, scanned)
        return Schema(root, scanned=scanned, total_records=total, sample=sample, complete=bool(complete),
                      elapsed=float(elapsed), skipped=int(skipped), truncated=self.truncated)


def _finish(a: _Acc, base: int, ctx: int) -> FieldInfo:
    """FieldInfo of accumulator ``a``; ``base`` = its context count, ``ctx`` = the context
    count of its children."""
    types = {TYPE_NAMES[i]: a.tc[i] for i in sorted(range(6), key=lambda i: (-a.tc[i], i)) if a.tc[i]}
    num_range = None
    if a.nlo_v is not None:
        num_range = (codec.number_text(a.nlo_v), codec.number_text(a.nhi_v))
    fi = FieldInfo(
        path=a.path, text=a.text, depth=a.depth, count=a.count, records=a.records, present=a.present, base=base,
        presence=(a.present / base) if base else 0.0, types=types, examples=list(a.examples),
        str_len=(a.slo, a.shi) if a.shi >= 0 else None, num_range=num_range,
        arr_len=(a.alo, a.ahi) if a.ahi >= 0 else None, obj_keys=(a.klo, a.khi) if a.khi >= 0 else None,
        is_key=a.is_key,
    )
    for seg, c in a.children.items():
        fi.children[seg] = _finish(c, ctx, c.count if c.elem else ctx)
    return fi


# ----------------------------------------------------------------------------------
# results
# ----------------------------------------------------------------------------------
def _seg_to_json(seg: Any) -> Any:
    if isinstance(seg, Wild):
        return {"wild": seg.value}
    return seg


def _seg_from_json(seg: Any) -> Any:
    if isinstance(seg, dict):
        return Wild(seg["wild"])
    if isinstance(seg, (str, int)) and not isinstance(seg, bool):
        return seg
    raise ValueError(f"invalid path segment {seg!r}")


def _pair(v: Any) -> tuple | None:
    if v is None:
        return None
    a, b = v
    return a, b


class FieldInfo:
    """Statistics of one path pattern (see the module docstring for the exact rules).

    * ``path``: tuple of ``str`` keys and :class:`~jsonreshape.paths.Wild` segments
      (``ANY_INDEX`` for array elements, ``ANY_KEY`` for pooled keys); ``()`` = the record;
    * ``text``: dotted text with ``[*]`` (``orders[*].id``; ``$`` = the record, ``$key`` =
      the member key); ``depth`` = ``len(path)``; ``is_key``: the ``$key`` pseudo-field;
    * ``count`` (occurrences), ``records`` (records holding it), ``present`` / ``base`` /
      ``presence`` (= present / base, 0..1);
    * ``types``: ``{type name: occurrences}``, most frequent first;
    * ``examples``: up to 5 distinct example texts;
    * ``str_len``, ``arr_len``, ``obj_keys``: ``(min, max)`` or ``None``;
      ``num_range``: ``(min_text, max_text)`` (exact number texts) or ``None``;
    * ``children``: ``{segment: FieldInfo}`` in first-seen order (``RECORD_KEY`` for
      ``$key``).
    """

    __slots__ = ("path", "text", "depth", "count", "records", "present", "base", "presence", "types",
                 "examples", "str_len", "num_range", "arr_len", "obj_keys", "children", "is_key")

    def __init__(self, path: tuple, text: str, depth: int, *, count: int = 0, records: int = 0, present: int = 0,
                 base: int = 0, presence: float = 0.0, types: dict[str, int] | None = None,
                 examples: list[str] | None = None, str_len: tuple[int, int] | None = None,
                 num_range: tuple[str, str] | None = None, arr_len: tuple[int, int] | None = None,
                 obj_keys: tuple[int, int] | None = None, is_key: bool = False) -> None:
        self.path = tuple(path)
        self.text = text
        self.depth = depth
        self.count = count
        self.records = records
        self.present = present
        self.base = base
        self.presence = presence
        self.types: dict[str, int] = dict(types or {})
        self.examples: list[str] = list(examples or [])
        self.str_len = str_len
        self.num_range = num_range
        self.arr_len = arr_len
        self.obj_keys = obj_keys
        self.children: dict[Any, FieldInfo] = {}
        self.is_key = is_key

    def __repr__(self) -> str:
        return f"FieldInfo({self.text!r}, count={self.count}, presence={self.presence:.3f}, types={self.types})"

    # -- derived -------------------------------------------------------------------------
    @property
    def missing(self) -> int:
        """Contexts (records / enclosing elements) without the field."""
        return max(0, self.base - self.present)

    @property
    def nulls(self) -> int:
        return self.types.get("null", 0)

    @property
    def main_type(self) -> str | None:
        """The most frequent type (``None`` when never seen)."""
        return next(iter(self.types), None)

    def type_fraction(self, name: str) -> float:
        """Share of the occurrences with JSON type ``name`` (0..1)."""
        return self.types.get(name, 0) / self.count if self.count else 0.0

    def iter_fields(self) -> Iterator[FieldInfo]:
        """The descendants in pre-order (this node excluded)."""
        stack = list(reversed(self.children.values()))
        while stack:
            fi = stack.pop()
            yield fi
            stack.extend(reversed(fi.children.values()))

    # -- serialization -------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable statistics (children not included; see Schema.to_dict)."""
        return {
            "path": [_seg_to_json(s) for s in self.path],
            "text": self.text,
            "key": self.is_key,
            "depth": self.depth,
            "count": self.count,
            "records": self.records,
            "present": self.present,
            "base": self.base,
            "presence": self.presence,
            "types": dict(self.types),
            "examples": list(self.examples),
            "str_len": None if self.str_len is None else list(self.str_len),
            "num_range": None if self.num_range is None else list(self.num_range),
            "arr_len": None if self.arr_len is None else list(self.arr_len),
            "obj_keys": None if self.obj_keys is None else list(self.obj_keys),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FieldInfo:
        """Inverse of :meth:`to_dict` (children are attached by :meth:`Schema.from_dict`)."""
        return cls(
            tuple(_seg_from_json(s) for s in data["path"]), str(data["text"]), int(data["depth"]),
            count=int(data["count"]), records=int(data["records"]), present=int(data["present"]),
            base=int(data["base"]), presence=float(data["presence"]),
            types={str(k): int(v) for k, v in data["types"].items()},
            examples=[str(e) for e in data["examples"]], str_len=_pair(data.get("str_len")),
            num_range=_pair(data.get("num_range")), arr_len=_pair(data.get("arr_len")),
            obj_keys=_pair(data.get("obj_keys")), is_key=bool(data.get("key", False)),
        )


class Schema:
    """Result of :func:`infer_schema`.

    * ``root``: :class:`FieldInfo` of the record value (path ``()``, text ``$``);
    * ``scanned``: records analysed; ``total_records``: records in the population;
      ``sample``: the requested sample size (``None`` = every record); ``complete``: every
      record of the population was analysed; ``elapsed``: seconds; ``skipped``: records
      that could not be analysed (logged); ``truncated``: a field limit was hit.
    """

    def __init__(self, root: FieldInfo, scanned: int, total_records: int, sample: int | None, complete: bool,
                 elapsed: float, skipped: int = 0, truncated: bool = False) -> None:
        self.root = root
        self.scanned = scanned
        self.total_records = total_records
        self.sample = sample
        self.complete = complete
        self.elapsed = elapsed
        self.skipped = skipped
        self.truncated = truncated
        self._fields: list[FieldInfo] | None = None
        self._by_text: dict[str, FieldInfo] | None = None

    def __repr__(self) -> str:
        return (f"<Schema {len(self.fields())} fields, {self.scanned}/{self.total_records} records"
                f"{' complete' if self.complete else ''}{' truncated' if self.truncated else ''}>")

    # -- access ----------------------------------------------------------------------------
    def fields(self) -> list[FieldInfo]:
        """Every field in pre-order (children in first-seen order), the root excluded."""
        if self._fields is None:
            self._fields = list(self.root.iter_fields())
        return list(self._fields)

    def find(self, path_text: str) -> FieldInfo | None:
        """The field at ``path_text``: its exact :attr:`FieldInfo.text` (``$`` = root,
        ``$key``), or any path syntax of :mod:`jsonreshape.paths` (``orders[*].id``,
        ``orders[0].id``, ``$.orders[*].id``, ``/orders/0/id``): array indices map to
        ``[*]``. ``None`` when there is no such field; ``PathSyntaxError`` for an invalid
        path."""
        if self._by_text is None:
            by_text = {self.root.text: self.root}
            for fi in self.fields():
                by_text.setdefault(fi.text, fi)
            self._by_text = by_text
        hit = self._by_text.get(path_text)
        if hit is None:
            hit = self._by_text.get(path_text.strip())
        if hit is not None:
            return hit
        node = self.root
        for seg in parse_pattern(path_text).segments:
            if seg is Wild.DESCENDANTS:
                return None
            if isinstance(seg, int) or seg is _ANY:
                nxt = node.children.get(_ANY)
            elif seg is _ANY_KEY:
                nxt = node.children.get(_ANY_KEY)
            else:
                nxt = node.children.get(seg)
                if nxt is None and canonical_index(seg) is not None:
                    nxt = node.children.get(_ANY)
            if nxt is None:
                return None
            node = nxt
        return node

    # -- serialization ---------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form: summary values, ``root`` (the record value) and
        ``fields`` (flat, pre-order). Numbers of the data appear only as exact texts."""
        return {
            "version": SCHEMA_VERSION,
            "scanned": self.scanned,
            "total_records": self.total_records,
            "sample": self.sample,
            "complete": self.complete,
            "elapsed": self.elapsed,
            "skipped": self.skipped,
            "truncated": self.truncated,
            "root": self.root.to_dict(),
            "fields": [fi.to_dict() for fi in self.fields()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Schema:
        """Rebuild a schema from :meth:`to_dict` output."""
        if int(data.get("version", SCHEMA_VERSION)) != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema version {data.get('version')!r}")
        root = FieldInfo.from_dict(data["root"])
        by_path: dict[tuple, FieldInfo] = {root.path: root}
        for item in data.get("fields") or ():
            fi = FieldInfo.from_dict(item)
            if fi.is_key:
                root.children[RECORD_KEY] = fi
                continue
            parent = by_path.get(fi.path[:-1])
            if parent is None or not fi.path:
                raise ValueError(f"field {fi.text!r} has no parent")
            parent.children[fi.path[-1]] = fi
            by_path[fi.path] = fi
        sample = data.get("sample")
        return cls(root, scanned=int(data["scanned"]), total_records=int(data["total_records"]),
                   sample=None if sample is None else int(sample), complete=bool(data["complete"]),
                   elapsed=float(data["elapsed"]), skipped=int(data.get("skipped", 0)),
                   truncated=bool(data.get("truncated", False)))

    # -- text output -------------------------------------------------------------------------
    def summary(self) -> str:
        """One localized line: records analysed, time, skipped/truncated notes (the English
        singular when the count is one)."""
        seconds = f"{self.elapsed:.2f}"
        if self.complete:
            text = t("schema.summary.full", scanned=_group(self.scanned), seconds=seconds)
        else:
            key = "schema.summary.sample.one" if self.total_records == 1 else "schema.summary.sample"
            text = t(key, scanned=_group(self.scanned), total=_group(self.total_records), seconds=seconds)
        if self.skipped:
            key = "schema.summary.skipped.one" if self.skipped == 1 else "schema.summary.skipped"
            text += " " + t(key, count=_group(self.skipped))
        if self.truncated:
            text += " " + t("schema.summary.truncated", fields=_group(len(self.fields())))
        return text

    def format_table(self, width: int | None = None, max_examples: int = 3) -> str:
        """A localized, aligned plain-text table for the CLI: path, types with their share,
        presence, examples. ``width``: maximum line width in terminal cells (``None`` = no
        limit; widths below ~40 cells cannot always be met); cells are truncated with ``…``
        to fit, wide (East Asian) characters count two cells, combining marks none, and
        control/bidi characters are shown as ``\\uXXXX``. The record itself is listed as
        ``$`` unless every record is an object.
        """
        rows: list[tuple[str, str, str, str]] = []
        root = self.root
        items = self.fields()
        if root.count and set(root.types) != {"object"}:
            items.insert(0, root)
        for fi in items:
            types = ", ".join(f"{t('schema.type.' + name)} {_pct(n / fi.count if fi.count else 0.0)}"
                              for name, n in fi.types.items())
            examples = ", ".join(fi.examples[:max(0, int(max_examples))])
            rows.append((_escape_display(fi.text), types, _pct(fi.presence), _escape_display(examples)))
        header = (t("schema.col.path"), t("schema.col.types"), t("schema.col.presence"), t("schema.col.examples"))
        widths = [max([_width(header[c])] + [_width(r[c]) for r in rows]) for c in range(4)]
        if width is not None:
            widths = _fit_widths(widths, int(width))
        lines = [_format_row(header, widths), "  ".join("-" * w for w in widths).rstrip()]
        for r in rows:
            lines.append(_format_row(r, widths))
        if not rows:
            lines.append(t("schema.no_records") if not self.scanned else t("schema.no_fields"))
        return "\n".join(lines)


# ----------------------------------------------------------------------------------
# table helpers
# ----------------------------------------------------------------------------------
def _group(n: int) -> str:
    text = f"{int(n):,}"
    return text.replace(",", " ") if get_language() == "uz" else text


def _pct(x: float) -> str:
    if x >= 1.0:
        return "100%"
    if x <= 0.0:
        return "0%"
    v = x * 100.0
    if v < 0.1:
        return "<0.1%"
    if v > 99.9:
        return ">99.9%"
    text = f"{v:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return text + "%"


def _char_width(ch: str) -> int:
    if ord(ch) < 0x300:
        return 1
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _width(s: str) -> int:
    """Terminal cells of ``s`` (wide characters 2, combining marks 0)."""
    if s.isascii():
        return len(s)
    return sum(_char_width(ch) for ch in s)


def _fit(s: str, w: int) -> str:
    """``s`` cut to ``w`` cells (with an ellipsis when cut)."""
    if _width(s) <= w:
        return s
    if w <= 0:
        return ""
    out = []
    used = 0
    for ch in s:
        cw = _char_width(ch)
        if used + cw > w - 1:
            break
        out.append(ch)
        used += cw
    return "".join(out) + ELLIPSIS


_COMFORT = (40, 28, 0, 30)    # path, types, (presence: natural), examples
_SHRINK = ((3, 10), (0, 20), (1, 12), (3, 4), (0, 8), (1, 6), (0, 1), (1, 1), (3, 1))


def _fit_widths(natural: list[int], total: int) -> list[int]:
    """Column widths for a table at most ``total`` cells wide: every column first gets a
    comfortable width (path 40, types 28, examples 30, presence its natural width); spare
    cells go to the path, then the examples, then the types (up to their natural widths);
    when even that is too wide, the examples shrink first, then the path, then the types."""
    w = [natural[c] if not _COMFORT[c] else min(natural[c], _COMFORT[c]) for c in range(4)]
    spare = total - 2 * 3 - sum(w)
    if spare >= 0:
        for c in (0, 3, 1):
            add = min(spare, natural[c] - w[c])
            w[c] += add
            spare -= add
        return w
    excess = -spare
    for c, low in _SHRINK:
        if excess <= 0:
            break
        cut = min(excess, max(0, w[c] - low))
        w[c] -= cut
        excess -= cut
    return w


def _format_row(cells: tuple[str, ...] | list[str], widths: list[int]) -> str:
    parts = []
    for c, (cell, w) in enumerate(zip(cells, widths)):
        text = _fit(cell, w)
        pad = " " * max(0, w - _width(text))
        parts.append(pad + text if c == 2 else text + pad)
    return "  ".join(parts).rstrip()


# ----------------------------------------------------------------------------------
# sampling
# ----------------------------------------------------------------------------------
def sample_positions(n: int, sample: int | None) -> list[int]:
    """Positions of the sampled items among ``n`` (see the module docstring): all when
    ``sample`` is ``None`` or ``n <= sample``, else ``j * (n - 1) // (sample - 1)``."""
    n = int(n)
    if sample is None or n <= sample:
        return list(range(max(0, n)))
    if sample == 1:
        return [0]
    span = n - 1
    den = sample - 1
    return [j * span // den for j in range(sample)]


def _sample_iterable(source: Iterable[int], sample: int, cancel: CancelToken | None) -> tuple[list[int], int]:
    """(chosen record indices, population size) of an iterable without ``len()``."""
    kept: list[int] = []
    stride = 1
    n = 0
    last = 0
    limit = 2 * sample + 2
    for idx in source:
        if n % stride == 0:
            kept.append(idx)
            if len(kept) >= limit:
                kept = kept[::2]
                stride *= 2
        last = idx
        n += 1
        if n & 0xFFFF == 0:
            check(cancel)
    if n <= sample:
        return kept, n
    if sample == 1:
        return kept[:1], n
    positions = sample_positions(n, sample)
    chosen = [kept[p // stride] for p in positions[:-1]]
    chosen.append(last)
    return chosen, n


def _is_sequence(source: Any) -> bool:
    return (hasattr(source, "__len__") and hasattr(source, "__getitem__")
            and not isinstance(source, (dict, str, bytes, bytearray)))


def _check_sample(sample: Any) -> int | None:
    if sample is None:
        return None
    if isinstance(sample, bool) or not isinstance(sample, int) or sample < 1:
        raise UserInputError("schema.bad_sample", value=repr(sample))
    return int(sample)


# ----------------------------------------------------------------------------------
# infer_schema
# ----------------------------------------------------------------------------------
def _records_extent(doc: Document) -> tuple[int, int]:
    """(bytes from the first record's start to the last record's end, records)."""
    count = doc.record_count
    if not count:
        return 0, 0
    return doc.record_span(count - 1)[1] - doc.record_span(0)[0], count


_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _io_pool() -> ThreadPoolExecutor:
    """The shared read-ahead pool (persistent, so that each Document keeps at most
    ``PREFETCH_THREADS`` thread-local read handles; they close with the Document)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=PREFETCH_THREADS, thread_name_prefix="jsonreshape-schema-io")
        return _pool


def _sample_blocks(doc: Document, chosen: Any, avg_bytes: float, cancel: CancelToken | None) -> Iterator[Block]:
    """Blocks of the sampled records, in sample order. A big sample of a file that is not
    in the OS cache is bound by random-read latency (~0.1 ms per record on NVMe), so its
    chunks are read ahead by a few threads (the device serves the requests in parallel)
    while this thread analyses; the record bytes in flight stay around
    ``PREFETCH_WINDOW_BYTES`` (estimated from the average record size)."""
    n = len(chosen)
    if n < PREFETCH_MIN_RECORDS:
        yield from doc.iter_blocks(chosen, block_bytes=SAMPLE_BLOCK_BYTES, cancel=cancel)
        return
    avg = max(1.0, avg_bytes)
    per = max(1, min(PREFETCH_CHUNK_RECORDS, int(PREFETCH_CHUNK_BYTES / avg)))
    window = max(2, min(2 * PREFETCH_THREADS, int(PREFETCH_WINDOW_BYTES / (per * avg))))
    pool = _io_pool()

    def read(lo: int) -> list[Block]:
        return list(doc.iter_blocks(chosen[lo:lo + per], block_bytes=SAMPLE_BLOCK_BYTES, cancel=cancel))

    starts = iter(range(0, n, per))
    pending: deque[Future[list[Block]]] = deque(pool.submit(read, lo) for lo in islice(starts, window))
    try:
        while pending:
            blocks = pending.popleft().result()
            lo = next(starts, None)
            if lo is not None:
                pending.append(pool.submit(read, lo))
            yield from blocks
    finally:
        for fut in pending:
            fut.cancel()


class _Duplicate(Exception):
    """A key repeats in a big object (checked by hash): the record is analysed again with
    a plan for every big object (:meth:`_RecordWalker.object_plan`)."""


class _RecordError(Exception):
    """The record cannot be analysed (bytes that are not the JSON the index accepted)."""


_FAILED: Any = object()
_SKIP_ERRORS = (_RecordError,)
_DECODE_ERRORS = (JsonReshapeError, ValueError, RecursionError)


class _KeyHashes:
    """The key hashes seen in one big object: a ``set`` up to :data:`KEYSET_MAX` keys,
    then an open-addressing table in an ``array('Q')`` (16 to 32 bytes per key instead
    of ~70 in a set; hashes are stored modulo 2**64, 0 as 1) -- a repeated hash means
    "maybe a duplicate key" (the plan pass compares the keys themselves)."""

    __slots__ = ("_set", "_table", "_mask", "_n")

    def __init__(self) -> None:
        self._set: set[int] | None = set()
        self._table = array("Q")
        self._mask = 0
        self._n = 0

    def add(self, h: int) -> bool:
        """Add the hash ``h``; True when it was there already."""
        s = self._set
        if s is not None:
            if h in s:
                return True
            s.add(h)
            if len(s) > KEYSET_MAX:
                self._set = None
                self._resize(2 * len(s) + 2)
                for x in s:
                    self._put(x)
            return False
        return self._put(h)

    def _resize(self, slots: int) -> None:
        """A table of the next power of two >= ``slots`` slots, holding the same hashes."""
        size = 1
        while size < slots:
            size <<= 1
        m = size - 1
        t = array("Q", [0]) * size     # (no zeroed bytes object of the same size first)
        for x in self._table:
            if x:
                i = (x ^ (x >> 29)) & m
                while t[i]:
                    i = (i + 1) & m
                t[i] = x
        self._table = t
        self._mask = m

    def _put(self, h: int) -> bool:
        h &= 0xFFFFFFFFFFFFFFFF
        if not h:
            h = 1
        t = self._table
        m = self._mask
        i = (h ^ (h >> 29)) & m
        while True:
            v = t[i]
            if not v:
                t[i] = h
                self._n += 1
                if 2 * self._n > m:
                    self._resize(2 * self._n + 2)   # doubles: load 25..50 %
                return False
            if v == h:
                return True
            i = (i + 1) & m


class _RecordWalker:
    """Adds one big record to a builder exactly as ``builder.add(codec.loads(record))``
    would, without parsing it whole (module docstring, "Big records"). Offsets are file
    offsets; ``local``: ``(data, delta)`` when the record's bytes are in memory
    (``data[x - delta]`` is the byte at offset ``x``), else they are read from the file."""

    def __init__(self, builder: SchemaBuilder, doc: Document, cancel: CancelToken | None,
                 local: tuple[Any, int] | None = None) -> None:
        self.b = builder
        self.doc = doc
        self.cancel = cancel
        # the largest input of one codec.loads call: below STREAM_RECORD_BYTES and below the
        # huge-record size (a huge record is never parsed whole, nor a value as big)
        large = getattr(getattr(doc, "settings", None), "large_node_bytes", None)
        limit = int(STREAM_RECORD_BYTES) if large is None else min(int(STREAM_RECORD_BYTES), int(large) - 1)
        self.limit = max(1, limit)
        self.cap = min(int(PARSE_BATCH_BYTES), self.limit - 2)   # batch bytes without the brackets
        self.feed = max(1, int(FEED_BYTES))
        self.loads = codec.loads
        self.exact = False
        self.f: Any = None
        self.mem: memoryview | None = None
        self.delta = 0
        if local is not None:
            self.mem = memoryview(local[0])
            self.delta = int(local[1])

    def close(self) -> None:
        f, self.f = self.f, None
        self.mem = None
        if f is not None:
            f.close()

    # -- bytes -------------------------------------------------------------------------
    def read(self, s: int, e: int) -> Any:
        mem = self.mem
        if mem is not None:
            return mem[s - self.delta:e - self.delta]
        f = self.f
        if f is None:
            open_data = getattr(self.doc, "open_data", None)
            f = self.f = open_data(buffering=0) if open_data is not None else \
                open_shared(self.doc.data_path, "rb", buffering=0)
        return read_range(f, s, e)

    def children(self, s: int, e: int) -> Iterator[tuple[array, Any, int]]:
        """The direct children (object members ``"key": value``) of the container value at
        ``[s, e)`` as the structure scanner reports them (it validates the container on the
        way): ``(spans, buf, off)`` per report -- ``spans`` an ``array('Q')`` of ``(start,
        end)`` pairs in order; the bytes ``[a, b)`` are ``buf[a - off:b - off]`` when ``a >=
        off`` (else read them), valid until the next report is requested."""
        scanner = core.Scanner(MODE_CONTAINER, [], s)
        mem = self.mem
        keep = bytearray()     # file-backed: the bytes [keep_start, pos), for small children
        keep_start = s
        hold = self.limit + self.feed
        pos = s
        finished = False
        while not finished:
            if pos < e:
                check(self.cancel)
                stop = min(e, pos + self.feed)
                chunk = self.read(pos, stop)
                out = scanner.feed(chunk)
                if mem is None:
                    keep += chunk
                pos = stop
            else:
                out = scanner.finish()
                finished = True
            err = scanner.error
            if err is not None:
                raise _RecordError(f"{err[1]} at byte {err[0]}")
            if out:
                sp = array("Q")
                sp.frombytes(out)
                if mem is not None:
                    yield sp, mem, self.delta
                else:
                    yield sp, keep, keep_start
                    last = sp[-1]
                    if last > keep_start:
                        del keep[:last - keep_start]
                        keep_start = last
            if mem is None and len(keep) > hold:
                keep_start = pos   # a child bigger than the limit is under way: read it later
                keep = bytearray()

    def member_key(self, s: int, e: int, data: Any) -> tuple[str, int]:
        """(decoded key, value offset relative to ``s``) of the member at ``[s, e)``."""
        if data is not None:
            try:
                return split_member(data)
            except ValueError as exc:
                raise _RecordError(str(exc)) from None
        probe = _KEY_PROBE
        while True:
            stop = min(e, s + probe)
            try:
                return split_member(self.read(s, stop))
            except ValueError as exc:
                if stop >= e:
                    raise _RecordError(str(exc)) from None
                probe *= 8

    # -- values ------------------------------------------------------------------------
    def record(self, span: tuple[int, int], member: bool) -> None:
        """What :meth:`SchemaBuilder.add` does for the record at ``span``."""
        b = self.b
        s, e = span
        b._rec += 1
        b._unit += 1
        unit = b._unit
        if member:
            key, vo = self.member_key(s, e, None)
            b._add_key(key, unit)
            s += vo
        self.value(b.root, s, e, unit, unit, None)
        b.scanned += 1

    def value(self, f: _Acc, s: int, e: int, own: int, kids: int, data: Any) -> None:
        """``builder._visit(f, codec.loads(bytes[s:e]), own, kids)``: values up to the
        parse limit are parsed, bigger containers (and the ones the decoders refuse,
        e.g. nesting too deep for them) are walked child by child."""
        b = self.b
        if e - s <= self.limit:
            if data is None:
                data = self.read(s, e)
            try:
                v = self.loads(data)
            except _DECODE_ERRORS as exc:
                if not len(data) or data[0] not in (0x7B, 0x5B):
                    raise _RecordError(str(exc)) from None    # a scalar the decoder refuses
                v = _FAILED                                   # a container: walked below
            if v is not _FAILED:
                b._visit(f, v, own, kids)
                return
        first = data[0] if data is not None and len(data) else self.read(s, s + 1)[0]
        if first == 0x7B or first == 0x5B:     # { [
            b._enter(f, own)
            if first == 0x7B:
                f.tc[0] += 1
                self.walk_object(f, s, e, kids)
            else:
                f.tc[1] += 1
                self.walk_array(f, s, e, kids)
            return
        if first == 0x22:                       # "
            self.string(f, s, e, own)
            return
        if data is None:
            data = self.read(s, e)
        try:
            v = self.loads(data)       # a huge number token (rare): held once, like in the parsed record
        except _DECODE_ERRORS as exc:
            raise _RecordError(str(exc)) from None
        b._visit(f, v, own, kids)

    def string(self, f: _Acc, s: int, e: int, own: int) -> None:
        """A string token bigger than the parse limit, decoded piece by piece (never whole):
        its statistics need only its length in characters and its first characters. A piece
        ends where the decoder accepts it -- not inside an escape or a UTF-8 character --
        and never between the two escapes of a surrogate pair (they make one character)."""
        b = self.b
        step = max(32, min(self.limit, int(PARSE_BATCH_BYTES)) - 2)
        pos = s + 1
        end = e - 1
        n = 0
        head: list[str] = []
        hlen = 0
        want = _EXK_CHARS + 1           # enough for the example and its key (a cut example)
        while pos < end:
            check(self.cancel)
            stop = min(end, pos + step)
            chunk = bytes(self.read(pos, stop))
            cut = len(chunk)
            text: Any = None
            for _attempt in range(16):
                if cut <= 0:
                    break
                try:
                    text = self.loads(b'"' + chunk[:cut] + b'"')
                except _DECODE_ERRORS:
                    text = None
                    cut -= 1          # inside an escape or a UTF-8 character
                    continue
                if pos + cut < end and text and 0xD800 <= ord(text[-1]) <= 0xDBFF and cut > 6:
                    text = None       # a high surrogate escape: its low half may follow
                    cut -= 6
                    continue
                break
            if not isinstance(text, str):
                raise _RecordError(f"invalid string at byte {pos}")
            n += len(text)
            if hlen < want:
                head.append(text[:want - hlen])
                hlen += len(head[-1])
            pos += cut
        b._enter(f, own)
        f.tc[2] += 1
        if n < f.slo:
            f.slo = n
        if n > f.shi:
            f.shi = n
        if not f.exfull:
            _add_str_example(f, "".join(head))   # the whole string, or more than its example uses

    def _done(self, f: _Acc, n: int, deep: bool, is_map: bool) -> None:
        """Length / key count of a walked container (like ``_dict`` / ``_list``)."""
        if is_map:
            if n < f.klo:
                f.klo = n
            if n > f.khi:
                f.khi = n
        else:
            if n < f.alo:
                f.alo = n
            if n > f.ahi:
                f.ahi = n
        if n and deep:
            self.b.truncated = True

    def walk_array(self, f: _Acc, s: int, e: int, kids: int) -> None:
        b = self.b
        deep = f.depth >= b.max_depth
        n = 0
        c: _Acc | None = None
        started = False
        cap = self.cap
        limit = self.limit
        for sp, buf, off in self.children(s, e):
            m = len(sp)
            n += m >> 1
            if deep:
                continue
            if not started:
                started = True
                c = f.children.get(_ANY) or b._child(f, _ANY)
            if c is None:
                continue
            first = -1           # sp position of the batch's first element
            i = 0
            while i < m:
                cs = sp[i]
                ce = sp[i + 1]
                if ce - cs < cap and cs >= off:
                    if first >= 0 and ce - sp[first] > cap:
                        self._elements(c, sp, first, i, buf, off, kids)
                        first = -1
                    if first < 0:
                        first = i
                    i += 2
                    continue
                if first >= 0:
                    self._elements(c, sp, first, i, buf, off, kids)
                    first = -1
                b._unit += 1
                self.value(c, cs, ce, kids, b._unit, buf[cs - off:ce - off] if ce - cs <= limit and cs >= off else None)
                i += 2
            if first >= 0:
                self._elements(c, sp, first, m, buf, off, kids)
        self._done(f, n, deep, False)

    def _elements(self, c: _Acc, sp: array, first: int, stop: int, buf: Any, off: int, kids: int) -> None:
        """The consecutive small array elements ``sp[first:stop]``, parsed together (their
        bytes with the separators between them)."""
        check(self.cancel)
        b = self.b
        try:
            values = self.loads(b"[" + buf[sp[first] - off:sp[stop - 1] - off] + b"]")
        except _DECODE_ERRORS:
            values = _FAILED
        if values is not _FAILED:
            visit = b._visit
            for x in values:
                b._unit += 1
                visit(c, x, kids, b._unit)
            return
        for j in range(first, stop, 2):
            b._unit += 1
            self.value(c, sp[j], sp[j + 1], kids, b._unit, buf[sp[j] - off:sp[j + 1] - off])

    def walk_object(self, f: _Acc, s: int, e: int, kids: int) -> None:
        seen: _KeyHashes | None = None    # optimistic walk: repeated key hashes raise _Duplicate
        if self.exact:
            plan = self.object_plan(s, e)
            if plan is not None:
                self._walk_planned(f, s, e, kids, plan)
                return
            # no key repeats (equal hashes of different keys at most): nothing to check
        else:
            seen = _KeyHashes()
        b = self.b
        deep = f.depth >= b.max_depth
        ch = f.children
        n = 0
        cap = self.cap
        limit = self.limit
        for sp, buf, off in self.children(s, e):
            m = len(sp)
            first = -1           # sp position of the batch's first member
            i = 0
            while i < m:
                cs = sp[i]
                ce = sp[i + 1]
                small = ce - cs <= limit and cs >= off
                if deep:
                    key = self.member_key(cs, ce, buf[cs - off:ce - off] if small else None)[0]
                    if seen is not None and seen.add(hash(key)):
                        raise _Duplicate()
                    n += 1
                    i += 2
                    continue
                if ce - cs < cap and cs >= off:
                    if first >= 0 and ce - sp[first] > cap:
                        n += self._members(f, sp, first, i, buf, off, kids, seen)
                        first = -1
                    if first < 0:
                        first = i
                    i += 2
                    continue
                if first >= 0:
                    n += self._members(f, sp, first, i, buf, off, kids, seen)
                    first = -1
                data = buf[cs - off:ce - off] if small else None
                key, vo = self.member_key(cs, ce, data)
                if seen is not None and seen.add(hash(key)):
                    raise _Duplicate()
                n += 1
                c = ch.get(key) or b._child(f, key)
                if c is not None:
                    self.value(c, cs + vo, ce, kids, kids, None if data is None else data[vo:])
                i += 2
            if first >= 0:
                n += self._members(f, sp, first, m, buf, off, kids, seen)
        self._done(f, n, deep, True)

    def _members(self, f: _Acc, sp: array, first: int, stop: int, buf: Any, off: int, kids: int,
                 seen: _KeyHashes | None) -> int:
        """The consecutive small members ``sp[first:stop]``, parsed together as one object
        (duplicates among them: dict semantics, as in the whole object); returns the number
        of distinct keys."""
        check(self.cancel)
        b = self.b
        ch = f.children
        try:
            d = self.loads(b"{" + buf[sp[first] - off:sp[stop - 1] - off] + b"}")
        except _DECODE_ERRORS:
            d = _FAILED
        if d is not _FAILED:
            visit = b._visit
            for k, x in d.items():
                if seen is not None and seen.add(hash(k)):
                    raise _Duplicate()
                c = ch.get(k) or b._child(f, k)
                if c is not None:
                    visit(c, x, kids, kids)
            return len(d)
        n = 0
        for j in range(first, stop, 2):
            cs = sp[j]
            ce = sp[j + 1]
            data = buf[cs - off:ce - off]
            key, vo = self.member_key(cs, ce, data)
            if seen is not None and seen.add(hash(key)):
                raise _Duplicate()
            n += 1
            c = ch.get(key) or b._child(f, key)
            if c is not None:
                self.value(c, cs + vo, ce, kids, kids, data[vo:])
        return n

    def _keys(self, s: int, e: int) -> Iterator[tuple[int, int, str, int, Any]]:
        """``(start, end, key, value offset, bytes or None)`` of the members of the big
        object at ``[s, e)``, in order."""
        limit = self.limit
        for sp, buf, off in self.children(s, e):
            for i in range(0, len(sp), 2):
                cs = sp[i]
                ce = sp[i + 1]
                data = buf[cs - off:ce - off] if ce - cs <= limit and cs >= off else None
                key, vo = self.member_key(cs, ce, data)
                yield cs, ce, key, vo, data

    def object_plan(self, s: int, e: int) -> dict[str, list[int]] | None:
        """``None`` when no key of the big object at ``[s, e)`` repeats, else ``{key:
        [first member position, last value start, last value end, occurrences]}`` of the
        repeated keys (dict semantics: the last value, at the first key's position)."""
        seen = _KeyHashes()
        repeated: set[int] = set()
        for _cs, _ce, key, _vo, _data in self._keys(s, e):
            h = hash(key)
            if seen.add(h):
                repeated.add(h)
        if not repeated:
            return None
        plan: dict[str, list[int]] = {}
        j = 0
        for cs, ce, key, vo, _data in self._keys(s, e):
            if hash(key) in repeated:
                p = plan.get(key)
                if p is None:
                    plan[key] = [j, cs + vo, ce, 1]
                else:
                    p[1] = cs + vo
                    p[2] = ce
                    p[3] += 1
            j += 1
        plan = {k: p for k, p in plan.items() if p[3] > 1}   # equal hashes of different keys dropped
        return plan or None

    def _walk_planned(self, f: _Acc, s: int, e: int, kids: int, plan: dict[str, list[int]]) -> None:
        """A big object with repeated keys, member by member following ``plan``."""
        b = self.b
        deep = f.depth >= b.max_depth
        ch = f.children
        n = 0
        j = -1
        for cs, ce, key, vo, data in self._keys(s, e):
            j += 1
            p = plan.get(key)
            if p is not None:
                if p[0] != j:
                    continue
                vs, ve, vdata = p[1], p[2], None
            else:
                vs, ve, vdata = cs + vo, ce, (None if data is None else data[vo:])
            n += 1
            if deep:
                continue
            c = ch.get(key) or b._child(f, key)
            if c is not None:
                self.value(c, vs, ve, kids, kids, vdata)
        self._done(f, n, deep, True)


def _stream_into(builder: SchemaBuilder, doc: Document, span: tuple[int, int], member: bool,
                 cancel: CancelToken | None, local: tuple[Any, int] | None = None) -> bool:
    """Add the big record at ``span`` (file offsets) to ``builder`` exactly as the parsed
    path would, without parsing it whole (:class:`_RecordWalker`). All or nothing: when
    the record cannot be analysed every change is undone and False is returned (logged);
    cancellation and file errors are raised (the record undone as well). ``local``: the
    record's bytes in memory (``(data, delta)``, see :class:`_RecordWalker`)."""
    walker = _RecordWalker(builder, doc, cancel, local)
    try:
        builder._begin()
        try:
            try:
                walker.record(span, member)
            except _Duplicate:
                # a key repeats in a big object: again, with a plan for every big object
                builder._rollback()
                builder._begin()
                walker.exact = True
                walker.record(span, member)
        except BaseException:
            builder._rollback()
            raise
        builder._commit()
        return True
    except _SKIP_ERRORS as exc:
        log.warning("schema: record at bytes %d..%d skipped: %s", span[0], span[1], exc)
        return False
    finally:
        walker.close()


def _analyse_block(builder: SchemaBuilder, doc: Document, blk: Block,
                   cancel: CancelToken | None) -> tuple[int, int, int, int]:
    """Add the records of ``blk``; returns (records skipped, bytes read, bytes of the
    records themselves, records). A contiguous block of sampled records also holds the
    small gaps between them, so the two byte counts differ."""
    member = doc.member_records
    if blk.huge:
        span = blk.span(0)
        ok = _stream_into(builder, doc, span, member, cancel)
        return (0 if ok else 1), span[1] - span[0], span[1] - span[0], 1
    skipped = 0
    sp = array("Q")
    sp.frombytes(blk.spans)
    data = blk.data
    loads = codec.loads
    add = builder.add

    def streamed(k: int) -> bool:
        span = doc.record_span(int(blk.indices[k]))
        rs, re_ = sp[2 * k], sp[2 * k + 1]
        local = (data, span[0] - rs) if span[1] - span[0] == re_ - rs else None
        return _stream_into(builder, doc, span, member, cancel, local)

    for k in range(len(blk.indices)):
        if k & (CHECK_RECORDS - 1) == CHECK_RECORDS - 1:
            check(cancel)
        if sp[2 * k + 1] - sp[2 * k] > STREAM_RECORD_BYTES:
            # a big record: parsing it would need ~25x its size in Python objects
            if not streamed(k):
                skipped += 1
            continue
        buf = data[sp[2 * k]:sp[2 * k + 1]]
        try:
            if member:
                key, vo = split_member(buf)
                value = loads(buf[vo:])
            else:
                key = None
                value = loads(buf)
        except (JsonReshapeError, ValueError, RecursionError):
            # valid JSON (the index checked it) that the decoders refuse, e.g. nesting
            # deeper than they support: walked child by child instead
            if not streamed(k):
                skipped += 1
            continue
        add(value, key)
    return skipped, len(data), sum(sp[1::2]) - sum(sp[0::2]), len(blk.indices)


def infer_schema(doc: Document, source: Any = None, *, sample: int | None = DEFAULT_SAMPLE,
                 progress: Progress | None = None, cancel: CancelToken | None = None) -> Schema:
    """Infer the structure of ``doc``'s records (see the module docstring).

    ``source``: ``None`` (every record), a ``range``, a sequence / RecordSet or an iterable
    of record indices; ``sample``: records to analyse (``None`` = every record; must be
    >= 1, else ``UserInputError("schema.bad_sample")``). The GUI passes
    ``doc.settings.sample_size``. Progress is reported in bytes; ``Cancelled`` when
    ``cancel`` fires.
    """
    started = time.perf_counter()
    sample = _check_sample(sample)
    if progress is not None and not progress.message:
        progress.set_message(t("schema.progress", name=doc.source_path.name))
    data_bytes = max(1, doc.data_size - doc.info.data_start)
    base_done = 0
    # -- the population --------------------------------------------------------------
    population: Any
    if source is None:
        if not doc.index_complete:
            sub = None
            if progress is not None:
                progress.set_total(2 * data_bytes)   # index + at most the whole data again
                sub = progress.child(0.0, 0.5)
            doc.build_index(progress=sub, cancel=cancel)
            base_done = data_bytes
        population = range(doc.record_count)
    elif isinstance(source, range) or _is_sequence(source):
        population = source
    else:
        population = None
    full: bool
    chosen: Any
    if population is not None:
        n_total = len(population)
        if sample is None or n_total <= sample:
            chosen, full = population, True
        else:
            chosen = array("Q", (int(population[p]) for p in sample_positions(n_total, sample)))
            full = False
    elif sample is None:
        chosen, full, n_total = source, True, -1
    else:
        picked, n_total = _sample_iterable(source, sample, cancel)
        chosen, full = array("Q", picked), len(picked) == n_total
    # -- progress total: the bytes of the analysed records -----------------------------
    # (a full pass over the document counts the bytes read -- the blocks cover the
    # records' extent exactly; any other pass counts the bytes of the analysed records
    # themselves, which is what the estimate is made of: the reader may read the small
    # gaps between sampled records too)
    extent, count = _records_extent(doc)
    whole = full and source is None
    avg = extent / count if count else 0.0
    n_chosen = len(chosen) if _is_sequence(chosen) else max(0, n_total)
    known = _is_sequence(chosen) or n_total >= 0   # an iterable of unknown length: no total
    est = extent if whole else int(n_chosen * avg)
    total: int | None = base_done + est if known else None
    if progress is not None:
        progress.set_total(total)
        progress.update(base_done)
    # -- the walk ----------------------------------------------------------------------
    builder = SchemaBuilder(max_fields=MAX_FIELDS, max_keys=MAX_KEYS, max_depth=MAX_DEPTH)
    skipped = done = seen = 0
    if full:
        blocks = doc.iter_blocks(chosen, block_bytes=BLOCK_BYTES, cancel=cancel)
    else:
        blocks = _sample_blocks(doc, chosen, avg, cancel)
    try:
        for blk in blocks:
            check(cancel)
            s, d_read, d_records, r = _analyse_block(builder, doc, blk, cancel)
            skipped += s
            done += d_read if whole else d_records
            seen += r
            if progress is not None:
                if total is not None and base_done + done > total:
                    # bigger records than the average: the remaining ones are estimated
                    # again, so that done never exceeds total
                    total = base_done + done + int(max(0, n_chosen - seen) * avg)
                    progress.set_total(total)
                progress.update(base_done + done)
        check(cancel)
    finally:
        blocks.close()   # stops a read-ahead at once on errors and cancellation
    if n_total < 0:
        n_total = seen
    ensure_unchanged = getattr(doc, "ensure_unchanged", None)
    if ensure_unchanged is not None:
        ensure_unchanged()   # results of a file another program changed meanwhile are not reported
    elapsed = time.perf_counter() - started
    complete = full and skipped == 0 and (source is not None or doc.index_complete)
    schema = builder.build(total_records=n_total, sample=sample, complete=complete, elapsed=elapsed,
                           skipped=skipped)
    if progress is not None:
        progress.finish()
    log.info("schema of %s: %d of %d records, %d fields, %.3f s%s%s", doc.source_path.name, schema.scanned,
             schema.total_records, len(schema.fields()), elapsed, f", {skipped} skipped" if skipped else "",
             ", truncated" if schema.truncated else "")
    return schema
