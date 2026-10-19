"""Structural diff of JSON values, nodes and whole files (docs/ARCHITECTURE.md §14.6; design:
scratchpad ``s5/map_repair_diff.md`` §4).

Entry points
------------
* :func:`diff_values` ``(a, b, opts)`` -- two parsed values (``codec.loads`` types) ->
  :class:`ValueDiff` (a list of :class:`Change`, complete counts, ``truncated``).
* :func:`diff_nodes` ``(doc_a, node_a, doc_b, node_b, opts, *, cancel)`` -- two nodes of
  documents of any size, memory-bounded through the reader's Node API.
* :func:`diff_files` / :func:`iter_record_diffs` -- two documents record by record (one
  :class:`RecordDiff` per differing record, a :class:`DiffReport` with the counts) in one of
  four modes; :func:`record_diff` -- the details of one record pair of such a run (the GUI's
  result grid computes them for the visible rows).
* :func:`write_diff` -- the differences as JSON, JSON Lines or CSV (:func:`record_dict` /
  :func:`summary_dict`: its objects, e.g. for JSON Lines on stdout); it never writes over a
  compared file (``DiffReport.sources``).
* :func:`align` -- the alignment of two sequences of hashable items (ops ``OP_EQ`` /
  ``OP_PAIRS`` / ``OP_DEL`` / ``OP_INS``), as the ``"auto"`` array matching uses it; the GUI's
  text comparison aligns lines with it.

Semantics (decision D11)
------------------------
Equality is the project's canonical equality (the duplicate finder's, :mod:`jsonreshape.dupes`):
numbers by exact decimal value (``1 == 1.0 == 1e0``, ``-0 == 0``, ``1e400 != 2e400``, integers
of any length), strings exactly (escapes decoded), ``true`` / ``false`` / ``null`` distinct
from numbers and strings (``1 != true``), objects without member order (duplicate keys: the
last wins, dict semantics), arrays ordered. ``DiffOptions(numbers="text")`` compares number
tokens textually instead (``1.10`` vs ``1.1`` is reported).

A :class:`Change` is ``added`` (only in B; ``a_path`` is ``None``), ``removed`` (only in A),
``changed`` (both present, not equal -- a type change included; two containers of the same kind
are never reported whole, the diff goes into them) or ``moved`` (an array element matched by
its key out of its relative order; array-key matching only). Paths are tuples of ``str`` keys
and ``int`` indices relative to the compared values; ``a_path`` and ``b_path`` differ where array
elements were aligned to other indices. Objects: A's members in A order, then the members only
in B in B order; changes come in document order (depth first). Values in a change are ``codec``
values (the whole subtree of an added / removed member); a value too large to hold -- a node
over :data:`VALUE_DIFF_MAX` in the Node path -- is an :class:`Omitted`. ``max_changes`` bounds
the stored changes of one value pair (``ValueDiff.truncated``); the counts are always complete.

Arrays (``DiffOptions.array_match``):

* ``"auto"`` (default): the elements are aligned by content -- equal elements anchor, the
  unequal stretches between anchors are paired position-wise and compared inside (git's
  "modified line"), so an insertion at the front is one added element, not a cascade. Common
  prefix and suffix first; then ``difflib.SequenceMatcher`` (no autojunk) when the rest has at
  most :data:`SM_PRODUCT_MAX` element pairs (2000 x 2000: its worst case is quadratic), else
  patience anchors (elements unique in both) on a longest increasing subsequence, recursively
  in the gaps. A part without any such anchor (every element repeated, e.g. booleans) gets a
  shortest edit script (Myers) when it needs at most :data:`MYERS_MAX_D` insertions and
  deletions, else its elements are paired by position. A part with more than
  :data:`PATIENCE_MAX` elements on a side takes a content-defined sample of about that many
  elements per side as anchor candidates (the low bits of the element's 64-bit hash are zero;
  equal elements are sampled on both sides alike), so memory stays bounded at any length and
  the gaps are aligned again. The identity of an element is a token: its ``orjson``
  serialization with sorted keys, ``JsonFloat`` / ``JsonInt`` numbers as their canonical
  decimal form (``numbers="text"``: as written). Equal tokens are always equal values; equal
  values spelled differently (``1`` vs ``1.0``) anchor less well but are still compared
  exactly.
* ``"index"``: element ``i`` with element ``i``; the longer tail is added / removed.
* ``"unordered"``: the order of the elements does not matter (a multiset): elements are matched
  by their whole value (canonical equality; the i-th of equal elements with the i-th), the rest
  is added / removed, nothing is ``moved`` (an element changed inside is removed + added). The
  Node path needs every element at most :data:`VALUE_DIFF_MAX` (else ``"auto"``).
* ``array_keys`` (entries ``"[PATH=]FIELD"``): the elements of the arrays at ``PATH`` (a
  pattern relative to the compared value; no ``PATH`` = every array) are matched by the value of
  ``FIELD`` (a concrete path inside the element, canonical equality). The i-th element with a
  key value pairs with the i-th element of B with the same value (duplicates), the rest is
  added / removed, and matched elements off a longest increasing subsequence of their B indices
  (taken in A order) are ``moved``. An array with an element that is not an object holding the
  field is aligned with ``"auto"`` instead. ``array_match="key"`` requires ``array_keys``;
  arrays no entry covers use ``"auto"``.

Files (:func:`diff_files`)
---------------------------
Both documents are pinned first (§12.3: an edited document is compared as shown, by positions;
``doc.pinned(state)`` of two states of one document compares them). ``mode``:

* ``"key"`` (``key=`` given, or ``"auto"`` for two documents of member records: ``$key``):
  streaming hash sort-merge, any size. One pass per document with :class:`dupes.KeyHasher` (the
  duplicate finder's key reading: PathExtractor / parse / ``$key`` / streamed huge records) and
  a 64-bit fingerprint of every record's compact form (``core.Reformatter`` + blake2b; a file
  holds them beyond :data:`FP_MEMORY_ENTRIES` per side); the ``(key hash << 64) | position``
  entries are sorted with :class:`dupes.HashRuns` (spilled runs beyond the memory limit) and
  merge-joined. In a group of equal keys the i-th record of A pairs with the i-th of B
  (positions ascending, decision D14); the surplus is removed / added and the group is counted
  in ``duplicate_keys`` (a warning). Records without the key are counted in ``missing_keys``
  (a warning) and compared with nothing; records whose key cannot be read likewise
  (``skipped_keys``). Pairs with equal fingerprints are equal without being parsed. The changed
  and removed records come in A order, then the added ones in B order. With ``$key`` the member
  VALUES are compared (the key is the identity); otherwise a member record (keyed documents,
  wrapped object containers) is compared as the one-member object ``{key: value}``.
* ``"align"`` (``"auto"`` without a key when both documents hold at most
  ``Settings.diff_align_max_bytes`` bytes of records; bigger: ``UserInputError(
  "diff.error.too_big_no_key")``, decision D13): the fingerprints of all records in memory
  (8 bytes per record and side), aligned like an ``"auto"`` array of their hashes; results in
  alignment order.
* ``"position"`` (any size): record ``i`` with record ``i``, the longer tail added / removed;
  sequential, constant memory.
* ``"document"`` (``"auto"`` for two single documents): the two roots through
  :func:`diff_nodes`; one :class:`RecordDiff` of scope ``"document"`` (indices ``None``).

Two wrapped documents also get their wrappers compared (in every mode but ``"document"``): the
text around the record container with the container replaced by ``null``; the changes are
yielded first as a :class:`RecordDiff` of scope ``"wrapper"`` and kept in
``DiffReport.wrapper``. A pair of records is parsed and walked when both are at most the value
limit ``min(VALUE_DIFF_MAX, Settings.large_node_bytes)``; bigger records go through the Node
path on their record nodes. ``max_details`` limits the record diffs that carry their changes
(``RecordDiff.diff``); every other pair is still decided exactly (the walk stops at the first
change) and counted.

Progress is reported in bytes of A + B (the passes; matching and comparing fill the rest of the
bar); cancellation is checked every block, batch and :data:`CHECK_EVERY` entries; both documents
are checked with ``ensure_unchanged()`` before the report is returned. ``results=True`` keeps a
compact result table for the GUI grid (``DiffReport.results_file``: one :data:`RESULT_ENTRY` per
differing record -- status code, A position, B position, the missing side as ``2**64 - 1``;
:meth:`DiffReport.read_results`); :meth:`DiffReport.close` removes it. Temporary files live in
``work_dir/sort-<pid>-<hex>/`` directories (removed at the end, also after an error or a
cancellation; ``recordset.purge_stale_files`` removes those of a crashed process).

Decisions (documented deviations / extensions)
-----------------------------------------------
* ``DiffReport.moved`` counts the ``moved`` changes inside the record diffs computed in detail;
  records themselves never "move" (key mode matches by key, the align mode reports a record out
  of order as removed + added).
* Node path: a string or number node over :data:`VALUE_DIFF_MAX` is compared by its raw text
  (escapes not decoded) and shown as :class:`Omitted`; an object with more than
  :data:`OBJECT_KEYS_MAX` members gives ``diff.error.too_many_keys`` (compare such data by key);
  array-key matching needs every element at most :data:`VALUE_DIFF_MAX` (else ``"auto"``).
* Record keys are matched with canonical equality also with ``numbers="text"``.
* Alignment thresholds: ``SequenceMatcher`` only up to :data:`SM_PRODUCT_MAX` element PAIRS
  (the design said "arrays up to 50 000 elements": its matching is quadratic when elements
  repeat -- 8.5 s for 10 000 booleans with 100 edits), patience beyond it, and a bounded Myers
  shortest edit script where patience finds no anchor (milliseconds for the same case); a part over
  :data:`PATIENCE_MAX` elements per side anchors on a content-defined sample instead of being
  paired by position (so align mode stays a content alignment for files of millions of small
  records, with bounded memory). The patience alignment works on runs of anchors with C-level
  containers (about 0.75 s for 840 000 records per side).
* Key mode: the entries of A and B share the key budget (``memory_limit``, default
  ``Settings.memory_limit_bytes // 4``) half and half -- they are held together until the
  merge-join, which frees them batch by batch while the changed pairs accumulate (those spill at
  the same size).
* ``RecordDiff.scope`` (``"record"`` / ``"wrapper"`` / ``"document"``), ``DiffReport.wrapper``,
  ``skipped_keys``, ``details``, ``results_count``, ``a_name``/``b_name``, ``a_gen``/``b_gen``,
  :func:`record_diff` (one pair's details for the GUI's result grid) and the keyword arguments
  ``results``, ``strategy``, ``block_bytes`` (and ``progress`` of :func:`diff_nodes`) extend the
  API of the design.
* Fix round R5 (review "files", R5F-2): :func:`write_diff` refuses every file the compared
  documents read -- their sources and data copies (gzip / UTF-16 inputs), under any spelling or
  a hard link (``writer.error.same_file``, the writer's rule) -- whether the documents are still
  open or closed: ``DiffReport.sources`` remembers them; the paths come from ``report=``, from
  the generator :func:`iter_record_diffs` returned (known when it is passed, before anything
  runs), from ``sources=``, and from the return value of any other generator of record diffs
  (checked at the end, before the result replaces anything). :func:`align` and the ``OP_*`` codes
  are public (the private names stay).
"""

from __future__ import annotations

import bisect
import collections
import csv
import difflib
import hashlib
import heapq
import io
import itertools
import logging
import mmap
import operator
import os
import secrets
import struct
import threading
import time
import weakref
from array import array
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Generator, Iterable, Iterator, Sequence

import orjson

from . import codec, core
from . import dupes as _dupes
from .appdirs import work_dir
from .codec import JsonFloat, JsonInt
from .dupes import (
    _BIT_OFFSETS,
    _NONZERO,
    ENTRY_BYTES,
    KEY_FIELD,
    HashRuns,
    KeyHasher,
    _check_strategy,
    _data_bytes,
    _u64,
    canonical_bytes,
    canonical_token,
    parse_key,
)
from .errors import JsonReshapeError, UserInputError
from .fileio import atomic_write, long_path, normalize_path, os_error, read_range, same_file
from .i18n import t
from .overlay import EDIT_BASE
from .paths import MISSING, PathPattern, format_path, get as path_get, parse_pattern
from .reader import split_member
from .recordset import _remove_tree, _warning_texts
from .settings import get_settings
from .stream import CaptureLimitExceeded
from .tasks import CancelToken, Progress, check, format_bytes

log = logging.getLogger(__name__)

__all__ = [
    "CHECK_EVERY",
    "Change",
    "DiffOptions",
    "DiffReport",
    "MODES",
    "MYERS_MAX_D",
    "OBJECT_KEYS_MAX",
    "OPS",
    "OP_DEL",
    "OP_EQ",
    "OP_INS",
    "OP_PAIRS",
    "Omitted",
    "PATIENCE_MAX",
    "RESULT_ENTRY",
    "RecordDiff",
    "SM_PRODUCT_MAX",
    "VALUE_DIFF_MAX",
    "ValueDiff",
    "align",
    "diff_files",
    "diff_nodes",
    "diff_values",
    "iter_record_diffs",
    "record_diff",
    "record_dict",
    "summary_dict",
    "write_diff",
]

KiB = 1 << 10
MiB = 1 << 20

VALUE_DIFF_MAX = 8 * MiB         # a node / record larger than this (per side) is not parsed whole
SM_PRODUCT_MAX = 4_000_000       # SequenceMatcher on at most this many element pairs (2000 x 2000)
PATIENCE_MAX = 1_000_000         # anchor candidates per side of one patience alignment (longer: sampled)
PATIENCE_CHUNK = 1 << 16         # items per C-level step of the patience alignment (short GIL holds)
MYERS_MAX_D = 1000               # edits a shortest-edit-script fallback may need (no patience anchors)
OBJECT_KEYS_MAX = 1_000_000      # members of one object the Node path can match by key
FP_MEMORY_ENTRIES = 1 << 20      # record fingerprints held in memory per side (key mode) before a file
BATCH_RECORDS = 1024             # record pairs read and compared together
BATCH_BYTES = 16 * MiB           # ... at most this many bytes of records
CHILD_WINDOW = 1024              # children fetched from a node at once
WINDOW_READ_MAX = 4 * MiB        # consecutive small children are read with one read up to this size
STREAM_CHUNK = 1 * MiB           # reads of a streamed fingerprint / raw comparison
CHECK_EVERY = 4096               # entries / records between cancellation checks
OMITTED_PREVIEW = 80             # characters of an Omitted value's preview
CSV_CELL_MAX = 32000             # characters of a value in a CSV cell (Excel's limit is 32767)
NO_INDEX = (1 << 64) - 1         # the missing side of a result entry
RESULT_ENTRY = struct.Struct("<BQQ")     # status code, A position, B position
STATUS_CODES = {"changed": 1, "added": 2, "removed": 3}
_CODE_STATUS = {v: k for k, v in STATUS_CODES.items()}
MODES = ("auto", "key", "align", "position", "document")
OPS = ("added", "removed", "changed", "moved")

_POS_MASK = (1 << 64) - 1
_FP_OPT = orjson.OPT_SORT_KEYS | orjson.OPT_PASSTHROUGH_SUBCLASS
_DECODE_ERRORS = (codec.JsonDecodeError, ValueError, RecursionError, UnicodeDecodeError)
_INF_KEY = 1 << 200
_ELLIPSIS = "…"

# alignment ops (lists of tuples): (_EQ, i, j, n) n equal elements; (_PAIRS, i, j, n) n unequal
# pairs (i+k, j+k); (_DEL, i, n) a[i:i+n] only in A; (_INS, j, n) b[j:j+n] only in B
_EQ, _PAIRS, _DEL, _INS = 0, 1, 2, 3
#: the op codes of :func:`align` (public names of ``_EQ`` / ``_PAIRS`` / ``_DEL`` / ``_INS``, which stay)
OP_EQ, OP_PAIRS, OP_DEL, OP_INS = _EQ, _PAIRS, _DEL, _INS

# kinds of parsed values
_K_NULL, _K_BOOL, _K_NUM, _K_STR, _K_ARR, _K_OBJ = range(6)
_KINDS: dict[type, int] = {
    type(None): _K_NULL, bool: _K_BOOL, int: _K_NUM, float: _K_NUM, JsonFloat: _K_NUM, JsonInt: _K_NUM,
    Decimal: _K_NUM, str: _K_STR, list: _K_ARR, tuple: _K_ARR, dict: _K_OBJ,
}
_NODE_KINDS = {"null": _K_NULL, "bool": _K_BOOL, "number": _K_NUM, "string": _K_STR, "array": _K_ARR,
               "object": _K_OBJ}


# ----------------------------------------------------------------------------------
# data structures
# ----------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Change:
    """One difference (module docstring): ``op`` (``added`` / ``removed`` / ``changed`` /
    ``moved``), ``a_path`` (``None`` for ``added``), ``b_path`` (``None`` for ``removed``),
    ``a`` / ``b`` the values (``MISSING`` where a side has none -- and for ``moved``)."""

    op: str
    a_path: tuple | None
    b_path: tuple | None
    a: Any = MISSING
    b: Any = MISSING

    @property
    def path(self) -> tuple:
        """The path to show: ``a_path``, or ``b_path`` for an added value."""
        return self.a_path if self.a_path is not None else self.b_path  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class Omitted:
    """A value too large to hold in a diff (a node over :data:`VALUE_DIFF_MAX`): its JSON kind,
    its size in bytes and a short preview of its text."""

    kind: str
    size: int
    preview: str = ""

    def text(self) -> str:
        """Localized description, e.g. ``[array, 120.0 MB]``."""
        return t("diff.value.omitted", kind=self.kind, size=format_bytes(self.size))


def _parse_array_key(item: Any) -> tuple[PathPattern | None, str]:
    """``"FIELD"`` / ``"PATH=FIELD"`` / ``(pattern or None, field)`` -> ``(PathPattern | None, field)``."""
    pattern: Any
    if isinstance(item, str):
        pattern, field_text = _split_array_key(item)
    elif isinstance(item, (tuple, list)) and len(item) == 2:
        pattern, field_text = item
    else:
        raise UserInputError("diff.error.array_key", value=repr(item))
    if not isinstance(field_text, str) or not field_text.strip():
        raise UserInputError("diff.error.array_key", value=repr(item))
    field_text = field_text.strip()
    fpat = parse_pattern(field_text)               # PathSyntaxError (localized) for bad syntax
    if not fpat.is_concrete:
        raise UserInputError("diff.error.array_key_wildcard", value=field_text)
    if pattern is None or isinstance(pattern, PathPattern):
        return pattern, field_text
    if isinstance(pattern, str):
        text = pattern.strip()
        return (parse_pattern(text) if text else None), field_text
    raise UserInputError("diff.error.array_key", value=repr(item))


def _split_array_key(text: str) -> tuple[str | None, str]:
    """``PATH=FIELD`` at the first ``=`` outside quotes and brackets; no ``=``: ``(None, text)``."""
    depth = 0
    quote = ""
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == "[":
            depth += 1
        elif c == "]":
            depth = max(0, depth - 1)
        elif c == "=" and depth == 0:
            return text[:i], text[i + 1:]
        i += 1
    return None, text


@dataclass
class DiffOptions:
    """How values are compared (module docstring): ``array_match`` ``"auto"`` / ``"index"`` /
    ``"key"`` / ``"unordered"``; ``array_keys`` entries ``"FIELD"`` / ``"PATH=FIELD"`` / ``(pattern, field)``
    (normalized to ``(PathPattern | None, field text)``); ``numbers`` ``"value"`` (alias
    ``"exact"``) / ``"text"``;
    ``max_changes`` the changes stored per value pair (``None``: no limit, ``0``: counts only).
    Invalid values raise ``UserInputError`` (``diff.error.*``) or ``PathSyntaxError``."""

    array_match: str = "auto"
    array_keys: tuple = ()
    numbers: str = "value"
    max_changes: int | None = 10000

    def __post_init__(self) -> None:
        am = self.array_match.strip().lower() if isinstance(self.array_match, str) else None
        if am not in ("auto", "index", "key", "unordered"):
            raise UserInputError("diff.error.array_match", value=repr(self.array_match))
        self.array_match = am
        nm = self.numbers.strip().lower() if isinstance(self.numbers, str) else None
        if nm == "exact":                  # an alias: numbers by their exact value
            nm = "value"
        if nm not in ("value", "text"):
            raise UserInputError("diff.error.numbers", value=repr(self.numbers))
        self.numbers = nm
        mc = self.max_changes
        if mc is not None and (isinstance(mc, bool) or not isinstance(mc, int) or mc < 0):
            raise UserInputError("diff.error.max_changes", value=repr(mc))
        raw = self.array_keys
        if raw is None:
            raw = ()
        elif isinstance(raw, str):
            raw = (raw,)
        self.array_keys = tuple(_parse_array_key(item) for item in raw)
        if am == "key" and not self.array_keys:
            raise UserInputError("diff.error.array_key_missing")
        self._rules: tuple[Any, list[tuple[PathPattern | None, tuple]]] | None = None

    def key_rules(self) -> list[tuple[PathPattern | None, tuple]]:
        """``array_keys`` with the field paths parsed (cached while ``array_keys`` is the same)."""
        cached = self._rules
        if cached is None or cached[0] is not self.array_keys:
            cached = self._rules = (self.array_keys,
                                    [(pat, tuple(parse_pattern(f).segments)) for pat, f in self.array_keys])
        return cached[1]


def _zero_counts() -> dict[str, int]:
    return dict.fromkeys(OPS, 0)


@dataclass
class ValueDiff:
    """The differences of one value pair: ``changes`` (at most ``max_changes``, in document
    order), ``truncated`` (more changes than stored), ``counts`` (every change by op)."""

    changes: list[Change] = field(default_factory=list)
    truncated: bool = False
    counts: dict[str, int] = field(default_factory=_zero_counts)

    @property
    def total(self) -> int:
        """Number of changes (stored or not)."""
        return sum(self.counts.values())

    @property
    def equal(self) -> bool:
        """No difference at all."""
        return self.total == 0


@dataclass
class RecordDiff:
    """A differing record (pair): ``status`` ``changed`` / ``added`` / ``removed``; ``a_index`` /
    ``b_index`` 0-based record numbers (positions of the pinned documents; ``None`` for the side
    without the record and for the ``wrapper`` / ``document`` scopes); ``key`` the key value
    shown (key mode: the ``--key`` value, ``$key``: the member key; ``MISSING`` otherwise and
    beyond ``max_details``); ``diff`` the changes (``None`` beyond ``max_details``; for an added /
    removed record one change holding the record); ``scope`` ``record`` / ``wrapper`` /
    ``document``."""

    status: str
    a_index: int | None
    b_index: int | None
    key: Any = MISSING
    diff: ValueDiff | None = None
    scope: str = "record"


@dataclass
class DiffReport:
    """Result of :func:`diff_files` (the generator of :func:`iter_record_diffs` returns it).

    ``mode`` (the mode used), ``key`` (the key text or ``None``), ``options``, ``a_records`` /
    ``b_records``, ``equal`` / ``changed`` / ``added`` / ``removed`` (records), ``moved``
    (element moves in the detailed record diffs), ``duplicate_keys`` / ``missing_keys`` /
    ``skipped_keys`` (per side, key mode: groups of a key occurring in more than one record,
    records without the key, records whose key could not be read), ``elapsed``, ``bytes_read``,
    ``warnings`` / ``warning_params`` (i18n keys; texts: :meth:`warning_texts`),
    ``results_file`` / ``results_count`` (``results=True``), ``wrapper`` (the wrappers'
    :class:`ValueDiff` when they differ), ``details`` (record diffs carrying their changes),
    ``sources`` (the files the two documents read -- sources and data copies, each once --, which
    :func:`write_diff` never writes over)."""

    mode: str
    key: str | None
    options: DiffOptions
    a_records: int = 0
    b_records: int = 0
    equal: int = 0
    changed: int = 0
    added: int = 0
    removed: int = 0
    moved: int = 0
    duplicate_keys: tuple[int, int] = (0, 0)
    missing_keys: tuple[int, int] = (0, 0)
    skipped_keys: tuple[int, int] = (0, 0)
    elapsed: float = 0.0
    bytes_read: int = 0
    warnings: list[str] = field(default_factory=list)
    warning_params: list[dict[str, Any]] = field(default_factory=list)
    results_file: Path | None = None
    results_count: int = 0
    wrapper: ValueDiff | None = None
    details: int = 0
    a_name: str = ""
    b_name: str = ""
    a_gen: int | None = None
    b_gen: int | None = None
    sources: tuple[Path, ...] = ()
    _temp: Any = field(default=None, repr=False, compare=False)

    @property
    def differences(self) -> int:
        """Differing records (+1 when the wrappers differ): 0 means "identical"."""
        return self.changed + self.added + self.removed + (1 if self.wrapper is not None else 0)

    @property
    def identical(self) -> bool:
        return self.differences == 0

    def warning_texts(self) -> list[str]:
        """The warnings in the current UI language."""
        return _warning_texts(self.warnings, self.warning_params)

    def read_results(self, start: int = 0, count: int | None = None) -> list[tuple[str, int | None, int | None]]:
        """Entries ``start ..`` of the result table as ``(status, a_index, b_index)``."""
        if self.results_file is None:
            return []
        start = max(0, int(start))
        n = self.results_count - start if count is None else min(int(count), self.results_count - start)
        if n <= 0:
            return []
        size = RESULT_ENTRY.size
        try:
            with open(long_path(self.results_file), "rb") as f:
                f.seek(start * size)
                data = f.read(n * size)
        except OSError as exc:
            raise os_error(exc, self.results_file) from exc
        out = []
        for code, a, b in RESULT_ENTRY.iter_unpack(data[:len(data) - len(data) % size]):
            out.append((_CODE_STATUS[code], None if a == NO_INDEX else a, None if b == NO_INDEX else b))
        return out

    def close(self) -> None:
        """Remove the result table (never raises)."""
        temp, self._temp = self._temp, None
        self.results_file = None
        if temp is not None:
            temp.cleanup()


# ----------------------------------------------------------------------------------
# comparing parsed values
# ----------------------------------------------------------------------------------
def _kind(v: Any) -> int:
    k = _KINDS.get(v.__class__)
    if k is not None:
        return k
    if isinstance(v, bool):
        return _K_BOOL
    if isinstance(v, (int, float, Decimal)):
        return _K_NUM
    if isinstance(v, str):
        return _K_STR
    if isinstance(v, dict):
        return _K_OBJ
    if isinstance(v, (list, tuple)):
        return _K_ARR
    raise TypeError(f"not a JSON value: {type(v).__name__}")


def _num_equal(x: Any, y: Any, text: bool) -> bool:
    """Numbers by exact value (plain int / float pairs compare exactly with ``==``: a plain float
    token is the shortest repr of its double, docs/ARCHITECTURE.md §1.3) or by token text."""
    if text:
        return codec.number_text(x) == codec.number_text(y)
    cx = x.__class__
    cy = y.__class__
    if (cx is int or cx is float) and (cy is int or cy is float):
        return x == y
    return canonical_bytes(x) == canonical_bytes(y)


def _leaf_equal(x: Any, y: Any, kind: int, text: bool) -> bool:
    if kind == _K_STR:
        return str.__eq__(x, y)    # type: ignore[no-any-return]
    if kind == _K_NUM:
        return _num_equal(x, y, text)
    if kind == _K_BOOL:
        return bool(x) is bool(y)
    return True                    # null


def _plain(obj: Any) -> Any:
    if isinstance(obj, bool):
        return bool(obj)
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return float(obj)
    if isinstance(obj, str):
        return str(obj)
    if isinstance(obj, dict):
        return dict(obj)
    if isinstance(obj, (list, tuple)):
        return list(obj)
    raise TypeError(f"not a JSON value: {type(obj).__name__}")


def _tok_value(obj: Any) -> Any:
    """``orjson`` default of the value tokens: exact numbers as their canonical form between
    ``\\x01`` bytes (never part of orjson's own output)."""
    if isinstance(obj, (JsonFloat, JsonInt)):
        return orjson.Fragment(b"\x01" + canonical_token(obj.raw.encode("ascii")) + b"\x01")
    if isinstance(obj, Decimal):
        return orjson.Fragment(b"\x01" + canonical_bytes(obj) + b"\x01")
    return _plain(obj)


def _tok_text(obj: Any) -> Any:
    """``orjson`` default of the text tokens: exact numbers as written."""
    if isinstance(obj, (JsonFloat, JsonInt)):
        return orjson.Fragment(obj.raw.encode("ascii"))
    if isinstance(obj, Decimal):
        return orjson.Fragment(str(obj).encode("ascii"))
    return _plain(obj)


def _token(v: Any, text: bool) -> bytes:
    """The identity of an array element for the alignment (module docstring): equal tokens are
    equal values (``numbers="text"``: equal texts of the numbers)."""
    try:
        # a compact copy: the alignment keeps one token per element, and an orjson result holds its whole
        # >= 4 KiB output buffer (100 000 elements: 390 MiB -> 7 MiB)
        return b"%b" % orjson.dumps(v, default=_tok_text if text else _tok_value, option=_FP_OPT)
    except TypeError:            # orjson.JSONEncodeError: lone surrogates, nesting deeper than orjson allows
        if text:
            return b"\x02" + codec.dumps(v, sort_keys=True)
        return b"\x03" + canonical_bytes(v)


def _hash64(data: bytes) -> int:
    return int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), "little")


# ----------------------------------------------------------------------------------
# alignment of two sequences of hashable, orderable items
# ----------------------------------------------------------------------------------
def _common_prefix(a: Sequence, b: Sequence, alo: int, blo: int, n: int) -> int:
    """Length of the common prefix of ``a[alo:]`` and ``b[blo:]`` within ``n`` items (slices
    are compared in growing chunks: C speed for long equal runs)."""
    k = 0
    chunk = 64
    while k < n:
        s = min(n - k, chunk)
        if a[alo + k:alo + k + s] == b[blo + k:blo + k + s]:
            k += s
            chunk = min(chunk * 2, 1 << 16)
            continue
        while a[alo + k] == b[blo + k]:
            k += 1
        return k
    return k


def _common_suffix(a: Sequence, b: Sequence, ahi: int, bhi: int, n: int) -> int:
    """Length of the common suffix of ``a[:ahi]`` and ``b[:bhi]`` within ``n`` items."""
    k = 0
    chunk = 64
    while k < n:
        s = min(n - k, chunk)
        if a[ahi - k - s:ahi - k] == b[bhi - k - s:bhi - k]:
            k += s
            chunk = min(chunk * 2, 1 << 16)
            continue
        while a[ahi - k - 1] == b[bhi - k - 1]:
            k += 1
        return k
    return k


def _pair_ops(ai: int, la: int, bj: int, lb: int, out: list[tuple]) -> None:
    k = min(la, lb)
    if k:
        out.append((_PAIRS, ai, bj, k))
    if la > k:
        out.append((_DEL, ai + k, la - k))
    if lb > k:
        out.append((_INS, bj + k, lb - k))


def _sm_ops(a: Sequence, b: Sequence, alo: int, ahi: int, blo: int, bhi: int, out: list[tuple]) -> None:
    sm = difflib.SequenceMatcher(None, a[alo:ahi], b[blo:bhi], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out.append((_EQ, alo + i1, blo + j1, i2 - i1))
        elif tag == "delete":
            out.append((_DEL, alo + i1, i2 - i1))
        elif tag == "insert":
            out.append((_INS, blo + j1, j2 - j1))
        else:
            _pair_ops(alo + i1, i2 - i1, blo + j1, j2 - j1, out)


def _lis(values: Sequence[int]) -> list[int]:
    """Indices (into ``values``) of a longest strictly increasing subsequence."""
    tails: list[int] = []
    tail_k: list[int] = []
    prev = array("q", bytes(8 * len(values)))
    bl = bisect.bisect_left
    for k, v in enumerate(values):
        p = bl(tails, v)
        if p == len(tails):
            tails.append(v)
            tail_k.append(k)
        else:
            tails[p] = v
            tail_k[p] = k
        prev[k] = tail_k[p - 1] if p else -1
    out: list[int] = []
    k = tail_k[-1] if tail_k else -1
    while k >= 0:
        out.append(k)
        k = prev[k]
    out.reverse()
    return out


def _sample_mask(n: int) -> int:
    """The candidate mask of a patience alignment over ``n`` items per side: 0 (every item is a
    candidate) up to :data:`PATIENCE_MAX`, else ``2**s - 1`` with ``2**s >= n / PATIENCE_MAX``: an
    item is a candidate when the low ``s`` bits of its hash are zero (about ``n / 2**s`` items)."""
    limit = max(1, int(PATIENCE_MAX))
    if n <= limit:
        return 0
    ratio = -(-n // limit)
    return (1 << (ratio - 1).bit_length()) - 1


def _candidates(seq: Sequence, lo: int, hi: int, mask: int, ints: bool) -> Iterator[tuple[Any, int]]:
    """``(item, index)`` of the candidate items of ``seq[lo:hi]`` in order: all of them when ``mask``
    is 0, else those whose hash -- the item itself for ints (record fingerprints and token hashes
    are uniform 64-bit hashes), else :func:`_hash64` -- has ``hash & mask == 0``. Content-defined:
    equal items are candidates on both sides or on none."""
    part = seq[lo:hi]
    pairs = zip(part, itertools.count(lo))
    if not mask:
        return pairs
    keys: Iterable[Any] = part if ints else map(_hash64, part)
    return itertools.compress(pairs, map(operator.not_, map(operator.and_, keys, itertools.repeat(mask))))


def _repeated(seq: Sequence, lo: int, hi: int, mask: int, ints: bool, index: dict[Any, int]) -> set[Any]:
    """The candidate items occurring more than once (``index``: the last index of every candidate)."""
    return {x for x, i in _candidates(seq, lo, hi, mask, ints) if index[x] != i}


def _patience(a: Sequence, b: Sequence, alo: int, ahi: int, blo: int, bhi: int,
              cancel: CancelToken | None) -> list[tuple[int, int, int]]:
    """The equal runs ``(i, j, n)`` (``a[i:i+n] == b[j:j+n]``; ascending on both sides) anchoring
    the alignment of ``a[alo:ahi]`` and ``b[blo:bhi]`` (patience diff): the candidate items that
    occur exactly once in each segment, on a longest increasing subsequence of their B indices
    taken in A order; consecutive anchors on one diagonal form one run, and two runs on one
    diagonal whose gap is equal on both sides are joined. Every item is a candidate up to
    :data:`PATIENCE_MAX` items per side; longer segments use a content-defined sample of about
    ``PATIENCE_MAX`` items per side (:func:`_sample_mask`), so the memory stays bounded, and the
    caller aligns the gaps between the runs again.

    The work is mostly C-level (``dict`` / ``set`` / ``map`` / ``compress`` / ``groupby`` / slice
    assignment). The longest increasing subsequence is computed over the runs of consecutive
    anchors: a run of consecutive B indices occupies consecutive slots of the patience piles, so
    it is placed with one ``bisect`` and two slice assignments, and the result is the
    subsequence the element-wise algorithm (:func:`_lis`) finds."""
    la, lb = ahi - alo, bhi - blo
    mask = _sample_mask(max(la, lb))
    ints = type(a[alo]) is int and type(b[blo]) is int
    # -- B: the index of every candidate; one occurring more than once is no anchor --------------
    # (in chunks of PATIENCE_CHUNK items: no single C call holds the GIL for long)
    where: dict[Any, int] = {}
    n_cand = 0
    for lo in range(blo, bhi, PATIENCE_CHUNK):
        chunk = list(_candidates(b, lo, min(bhi, lo + PATIENCE_CHUNK), mask, ints))
        n_cand += len(chunk)
        where.update(chunk)
        del chunk
        check(cancel)
    bad_j: set[int] = set()
    if len(where) != n_cand:
        bad_j = {where[x] for x in _repeated(b, blo, bhi, mask, ints, where)}
    # -- the candidates of A found in B, in A order: A indices (ia) and B indices (jb) -----------
    ia: list[int] = []
    jb: list[int] = []
    get = where.get
    if not mask:
        for lo in range(alo, ahi, PATIENCE_CHUNK):
            hi = min(ahi, lo + PATIENCE_CHUNK)
            js = list(map(get, a[lo:hi]))
            keep = list(map(operator.is_not, js, itertools.repeat(None)))
            ia.extend(itertools.compress(range(lo, hi), keep))
            jb.extend(itertools.compress(js, keep))
            check(cancel)
        del js, keep
    else:
        for x, i in _candidates(a, alo, ahi, mask, ints):
            j = get(x)
            if j is not None:
                ia.append(i)
                jb.append(j)
    del where, get
    check(cancel)
    # an item occurring more than once in A gives its B index more than once: no anchor either
    seen: set[int] = set()
    for k in range(0, len(jb), PATIENCE_CHUNK):
        seen.update(jb[k:k + PATIENCE_CHUNK])
    if len(seen) != len(jb):
        counts: collections.Counter[int] = collections.Counter()
        for k in range(0, len(jb), PATIENCE_CHUNK):
            counts.update(jb[k:k + PATIENCE_CHUNK])
        bad_j |= {j for j, c in counts.items() if c > 1}
        del counts
    del seen
    if bad_j:
        mask_keep = [j not in bad_j for j in jb]
        ia = list(itertools.compress(ia, mask_keep))
        jb = list(itertools.compress(jb, mask_keep))
        del mask_keep
    del bad_j
    check(cancel)
    n = len(ia)
    if not n:
        return []
    # -- runs of consecutive anchors on one diagonal: (i + t, j + t) ----------------------------
    starts: list[int] = []
    sizes: list[int] = []
    k = 0
    for _key, grp in itertools.groupby(zip(map(operator.sub, jb, ia), map(operator.sub, ia, range(n)))):
        c = len(list(grp))
        starts.append(k)
        sizes.append(c)
        k += c
    check(cancel)
    # -- the longest increasing subsequence of jb, run by run -----------------------------------
    tails: list[int] = []        # tails[p]: the smallest last B index of an increasing subsequence of p + 1 anchors
    tail_k: list[int] = []       # ... and the anchor ending it
    prev: list[int] = []         # per run: the anchor before its first one on the subsequence (-1: none)
    bl = bisect.bisect_left
    for r in range(len(starts)):
        if r % CHECK_EVERY == CHECK_EVERY - 1:
            check(cancel)
        k0 = starts[r]
        c = sizes[r]
        j0 = jb[k0]
        p = bl(tails, j0)
        prev.append(tail_k[p - 1] if p else -1)
        tails[p:p + c] = range(j0, j0 + c)
        tail_k[p:p + c] = range(k0, k0 + c)
    del tails
    runs: list[tuple[int, int, int]] = []
    k = tail_k[-1]
    del tail_k
    br = bisect.bisect_right
    while k >= 0:
        r = br(starts, k) - 1
        k0 = starts[r]
        runs.append((ia[k0], jb[k0], k - k0 + 1))
        k = prev[r]
    runs.reverse()
    check(cancel)
    # -- join the runs on one diagonal whose gap is equal on both sides -------------------------
    out: list[tuple[int, int, int]] = []
    for i, j, c in runs:
        if out:
            pi, pj, pc = out[-1]
            gap = i - pi - pc
            if gap == j - pj - pc and (gap == 0 or a[pi + pc:i] == b[pj + pc:j]):
                out[-1] = (pi, pj, pc + gap + c)
                continue
        out.append((i, j, c))
    return out


def _myers(a: Sequence, b: Sequence, alo: int, ahi: int, blo: int, bhi: int, max_d: int,
           cancel: CancelToken | None) -> list[tuple] | None:
    """Alignment ops of ``a[alo:ahi]`` / ``b[blo:bhi]`` from a shortest edit script (Myers'
    O(ND) greedy algorithm: the longest common subsequence), or ``None`` when it needs more than
    ``max_d`` insertions + deletions. The cost depends on the number of edits, not on how often
    items repeat -- the case the patience alignment has no anchors for and ``SequenceMatcher``
    is quadratic in. Diagonals ("snakes") are followed with slice comparisons; the deletions and
    insertions between two equal runs are paired position-wise (as a ``replace`` of
    :func:`_sm_ops`)."""
    n = ahi - alo
    m = bhi - blo
    off = max_d + 1
    v = [0] * (2 * max_d + 3)            # v[off + k]: the furthest x on diagonal k = x - y
    trace: list[list[int]] = []          # v[off-d-1 .. off+d+1] before step d (for the way back)
    end_d = -1
    for d in range(max_d + 1):
        check(cancel)
        trace.append(v[off - d - 1:off + d + 2])
        for k in range(-d, d + 1, 2):
            kk = off + k
            if k == -d or (k != d and v[kk - 1] < v[kk + 1]):
                x = v[kk + 1]                # down: an insertion
            else:
                x = v[kk - 1] + 1            # right: a deletion
            y = x - k
            if x < n and y < m and a[alo + x] == b[blo + y]:
                s = _common_prefix(a, b, alo + x, blo + y, min(n - x, m - y))
                x += s
                y += s
            v[kk] = x
            if x >= n and y >= m:
                end_d = d
                break
        if end_d >= 0:
            break
    if end_d < 0:
        return None
    # -- the way back: one edit and one snake per step -------------------------------------------
    steps: list[tuple] = []              # ("eq", x, y, n) / ("del", x) / ("ins", y), backwards
    x, y = n, m
    for d in range(end_d, 0, -1):
        t = trace[d]
        base = d + 1                     # t[base + k] = v[off + k] before step d
        k = x - y
        if k == -d or (k != d and t[base + k - 1] < t[base + k + 1]):
            px = t[base + k + 1]
            py = px - (k + 1)
            mx, my = px, py + 1
            edit: tuple = ("ins", py)
        else:
            px = t[base + k - 1]
            py = px - (k - 1)
            mx, my = px + 1, py
            edit = ("del", px)
        if x > mx:
            steps.append(("eq", mx, my, x - mx))
        steps.append(edit)
        x, y = px, py
    if x > 0:
        steps.append(("eq", 0, 0, x))
    steps.reverse()
    # -- to ops: the equal runs, and the edits between two of them paired ------------------------
    out: list[tuple] = []
    gi = gj = 0                          # where the current gap starts
    for st in steps:
        if st[0] == "eq":
            _, ex, ey, c = st
            _pair_ops(alo + gi, ex - gi, blo + gj, ey - gj, out)
            out.append((_EQ, alo + ex, blo + ey, c))
            gi, gj = ex + c, ey + c
    _pair_ops(alo + gi, n - gi, blo + gj, m - gj, out)
    return out


def align(a: Sequence, b: Sequence, cancel: CancelToken | None = None, alo: int = 0, ahi: int | None = None,
          blo: int = 0, bhi: int | None = None) -> list[tuple]:
    """Alignment ops covering ``a[alo:ahi]`` and ``b[blo:bhi]`` in order (module docstring
    "Arrays"); equal items (``==``) are equal elements. The items are hashable: ``bytes`` tokens,
    ``int`` hashes (a list or an ``array('Q')`` of fingerprints), text lines.

    The ops: ``(OP_EQ, i, j, n)`` -- ``n`` equal items ``a[i:i+n]`` / ``b[j:j+n]``; ``(OP_PAIRS, i,
    j, n)`` -- ``n`` unequal pairs ``(a[i+k], b[j+k])``; ``(OP_DEL, i, n)`` -- ``a[i:i+n]`` only in
    A; ``(OP_INS, j, n)`` -- ``b[j:j+n]`` only in B. ``cancel`` is checked per segment. (Public
    since fix round R5 -- the GUI's text comparison aligns lines with it; ``_align`` stays an
    alias.)"""
    out: list[tuple] = []
    work: list[tuple] = [("seg", alo, len(a) if ahi is None else ahi, blo, len(b) if bhi is None else bhi)]
    while work:
        item = work.pop()
        if item[0] == "op":
            out.append(item[1])
            continue
        _, alo_, ahi_, blo_, bhi_ = item
        check(cancel)
        n = _common_prefix(a, b, alo_, blo_, min(ahi_ - alo_, bhi_ - blo_))
        if n:
            out.append((_EQ, alo_, blo_, n))
            alo_ += n
            blo_ += n
        m = _common_suffix(a, b, ahi_, bhi_, min(ahi_ - alo_, bhi_ - blo_))
        if m:
            ahi_ -= m
            bhi_ -= m
            work.append(("op", (_EQ, ahi_, bhi_, m)))
        la = ahi_ - alo_
        lb = bhi_ - blo_
        if la == 0 or lb == 0:
            if la:
                out.append((_DEL, alo_, la))
            if lb:
                out.append((_INS, blo_, lb))
            continue
        if la * lb <= SM_PRODUCT_MAX:
            _sm_ops(a, b, alo_, ahi_, blo_, bhi_, out)
            continue
        runs = _patience(a, b, alo_, ahi_, blo_, bhi_, cancel)
        if not runs:
            # no item unique on both sides: a shortest edit script when it is short, else by position
            ops = _myers(a, b, alo_, ahi_, blo_, bhi_, MYERS_MAX_D, cancel)
            if ops is None:
                _pair_ops(alo_, la, blo_, lb, out)
            else:
                out.extend(ops)
            continue
        # the gaps between the equal runs are new segments
        segs: list[tuple] = []
        pi, pj = alo_, blo_
        for i, j, c in runs:
            if i > pi or j > pj:
                segs.append(("seg", pi, i, pj, j))
            segs.append(("op", (_EQ, i, j, c)))
            pi, pj = i + c, j + c
        if pi < ahi_ or pj < bhi_:
            segs.append(("seg", pi, ahi_, pj, bhi_))
        segs.reverse()
        work.extend(segs)
    return out


_align = align      # the name used inside this module and by gui/diffview.py before fix round R5


# ----------------------------------------------------------------------------------
# the walk over parsed values
# ----------------------------------------------------------------------------------
class _Stop(Exception):
    """The first change of a stop-at-first-change sink (equality only)."""


class _Sink:
    """Collects the changes of one comparison into a :class:`ValueDiff`."""

    __slots__ = ("vd", "limit", "stop")

    def __init__(self, limit: int | None, stop: bool = False) -> None:
        self.vd = ValueDiff()
        self.limit = limit
        self.stop = stop

    def add(self, op: str, a_path: tuple | None, b_path: tuple | None, a: Any = MISSING, b: Any = MISSING) -> None:
        vd = self.vd
        vd.counts[op] += 1
        if self.stop:
            raise _Stop
        if self.limit is None or len(vd.changes) < self.limit:
            vd.changes.append(Change(op, a_path, b_path, a, b))
        else:
            vd.truncated = True

    def emit(self, change: Change) -> None:
        self.add(change.op, change.a_path, change.b_path, change.a, change.b)


def _key_match(ka: Sequence[bytes], kb: Sequence[bytes]) -> tuple[list[tuple[int, int]], set[int]]:
    """Match element keys: the i-th element of A with a key value pairs with the i-th of B with
    the same value. Returns ``(pairs in A order, indices of the pairs NOT moved)``."""
    bmap: dict[bytes, list[int]] = {}
    for j, c in enumerate(kb):
        bmap.setdefault(c, []).append(j)
    used: dict[bytes, int] = {}
    pairs: list[tuple[int, int]] = []
    for i, c in enumerate(ka):
        lst = bmap.get(c)
        if lst is None:
            continue
        n = used.get(c, 0)
        if n < len(lst):
            pairs.append((i, lst[n]))
            used[c] = n + 1
    keep = set(_lis([j for _, j in pairs]))
    return pairs, keep


def _chain(path: tuple) -> Any:
    """A path as a chain of ``(parent, segment)`` pairs (``None`` = the root): a child's path
    costs one small tuple instead of a copy of the parent's path (deep values stay linear)."""
    c: Any = None
    for seg in path:
        c = (c, seg)
    return c


def _unchain(c: Any) -> tuple:
    segs = []
    while c is not None:
        c, seg = c
        segs.append(seg)
    segs.reverse()
    return tuple(segs)


class _Emit:
    """A change waiting on the walk's stack (paths as chains; the op says which sides exist)."""

    __slots__ = ("op", "ca", "cb", "a", "b")

    def __init__(self, op: str, ca: Any, cb: Any, a: Any = MISSING, b: Any = MISSING) -> None:
        self.op = op
        self.ca = ca
        self.cb = cb
        self.a = a
        self.b = b


class _Walker:
    """Compares two parsed values into a sink (iterative, any depth; ``cancel`` is checked every
    :data:`CHECK_EVERY` steps)."""

    def __init__(self, opts: DiffOptions, sink: _Sink, skip: int = 0, cancel: CancelToken | None = None) -> None:
        self.opts = opts
        self.sink = sink
        self.text = opts.numbers == "text"
        self.rules = opts.key_rules()
        self.skip = skip          # leading path segments not seen by array patterns (member records)
        self.cancel = cancel
        m = opts.array_match
        self.default = ("auto" if m == "key" else m), None

    def method(self, path: tuple) -> tuple[str, tuple | None]:
        """The array matching of the array at ``path`` (A side)."""
        if self.rules:
            rel = path[self.skip:]
            for pat, fpath in self.rules:
                if pat is None or pat.matches(rel):
                    return "key", fpath
        return self.default

    def run(self, a: Any, b: Any, pa: tuple = (), pb: tuple = ()) -> None:
        sink = self.sink
        text = self.text
        cancel = self.cancel
        stack: list[Any] = [(_chain(pa), _chain(pb), a, b)]
        pop = stack.pop
        steps = 0
        while stack:
            item = pop()
            steps += 1
            if cancel is not None and steps % CHECK_EVERY == 0:
                check(cancel)
            if item.__class__ is _Emit:
                op = item.op
                sink.add(op, None if op == "added" else _unchain(item.ca),
                         None if op == "removed" else _unchain(item.cb), item.a, item.b)
                continue
            ca, cb, x, y = item
            kx = _kind(x)
            ky = _kind(y)
            if kx != ky:
                sink.add("changed", _unchain(ca), _unchain(cb), x, y)
            elif kx == _K_OBJ:
                todo: list[Any] = []
                for k, vx in x.items():
                    vy = y.get(k, MISSING)
                    if vy is MISSING:
                        todo.append(_Emit("removed", (ca, k), None, vx, MISSING))
                    else:
                        todo.append(((ca, k), (cb, k), vx, vy))
                for k, vy in y.items():
                    if k not in x:
                        todo.append(_Emit("added", None, (cb, k), MISSING, vy))
                todo.reverse()
                stack.extend(todo)
            elif kx == _K_ARR:
                todo = self._array(x, y, ca, cb)
                todo.reverse()
                stack.extend(todo)
            elif not _leaf_equal(x, y, kx, text):
                sink.add("changed", _unchain(ca), _unchain(cb), x, y)

    def _array(self, x: list, y: list, ca: Any, cb: Any) -> list[Any]:
        method, fpath = self.method(_unchain(ca)) if self.rules else self.default
        if method == "unordered":
            return self._by_key(x, y, ca, cb, (), moves=False)    # type: ignore[return-value]
        if method == "key":
            todo = self._by_key(x, y, ca, cb, fpath)   # type: ignore[arg-type]
            if todo is not None:
                return todo
            method = "auto"
        if len(x) == 1 and len(y) == 1:
            # one element each: any alignment pairs them (no tokens: deep chains stay linear)
            return [((ca, 0), (cb, 0), x[0], y[0])]
        todo: list[Any] = []
        if method == "index":
            n = min(len(x), len(y))
            todo.extend(((ca, i), (cb, i), x[i], y[i]) for i in range(n))
            todo.extend(_Emit("removed", (ca, i), None, x[i], MISSING) for i in range(n, len(x)))
            todo.extend(_Emit("added", None, (cb, j), MISSING, y[j]) for j in range(n, len(y)))
            return todo
        text = self.text
        ta = [_token(v, text) for v in x]
        tb = [_token(v, text) for v in y]
        for op in _align(ta, tb, self.cancel):
            tag = op[0]
            if tag == _PAIRS:
                _, i, j, n = op
                todo.extend(((ca, i + k), (cb, j + k), x[i + k], y[j + k]) for k in range(n))
            elif tag == _DEL:
                _, i, n = op
                todo.extend(_Emit("removed", (ca, i + k), None, x[i + k], MISSING) for k in range(n))
            elif tag == _INS:
                _, j, n = op
                todo.extend(_Emit("added", None, (cb, j + k), MISSING, y[j + k]) for k in range(n))
        return todo

    def _by_key(self, x: list, y: list, ca: Any, cb: Any, fpath: tuple, moves: bool = True) -> list[Any] | None:
        ka = _element_keys(x, fpath)
        if ka is None:
            return None
        kb = _element_keys(y, fpath)
        if kb is None:
            return None
        pairs, keep = _key_match(ka, kb)
        pair_of = {i: (k, j) for k, (i, j) in enumerate(pairs)}
        matched_b = {j for _, j in pairs}
        todo: list[Any] = []
        for i, v in enumerate(x):
            hit = pair_of.get(i)
            if hit is None:
                todo.append(_Emit("removed", (ca, i), None, v, MISSING))
                continue
            k, j = hit
            if moves and k not in keep:
                todo.append(_Emit("moved", (ca, i), (cb, j)))
            todo.append(((ca, i), (cb, j), v, y[j]))
        todo.extend(_Emit("added", None, (cb, j), MISSING, y[j]) for j in range(len(y)) if j not in matched_b)
        return todo


def _element_keys(items: Sequence[Any], fpath: tuple) -> list[bytes] | None:
    """Canonical key values of the elements (``fpath`` ``()``: the element itself), ``None``
    when an element has none (not an object holding the field)."""
    out = []
    for v in items:
        if fpath:
            if not isinstance(v, dict):
                return None
            kv = path_get(v, fpath)
            if kv is MISSING:
                return None
        else:
            kv = v
        out.append(canonical_bytes(kv))
    return out


def diff_values(a: Any, b: Any, opts: DiffOptions | None = None) -> ValueDiff:
    """The differences between two parsed JSON values (module docstring)."""
    opts = _opts(opts)
    sink = _Sink(opts.max_changes)
    _Walker(opts, sink).run(a, b)
    return sink.vd


def _opts(opts: DiffOptions | None) -> DiffOptions:
    if opts is None:
        return DiffOptions()
    if not isinstance(opts, DiffOptions):
        raise TypeError(f"opts must be a DiffOptions, not {type(opts).__name__}")
    return opts


# ----------------------------------------------------------------------------------
# the Node path (memory-bounded)
# ----------------------------------------------------------------------------------
class _Lazy:
    """A stack item producing further items in order (bounded memory for wide containers)."""

    __slots__ = ("it",)

    def __init__(self, it: Iterator[Any]) -> None:
        self.it = it


def _compact_fp(raw: bytes) -> int:
    f = core.Reformatter(None)
    return _hash64(f.feed(raw) + f.finish())


class _NodeDiff:
    """Compares two nodes through the Node API (module docstring "Node path")."""

    def __init__(self, da: Any, db: Any, opts: DiffOptions, sink: _Sink, cancel: CancelToken | None,
                 skip: int = 0, progress: Progress | None = None) -> None:
        self.da = da
        self.db = db
        self.opts = opts
        self.sink = sink
        self.cancel = cancel
        self.text = opts.numbers == "text"
        self.walker = _Walker(opts, sink, skip, cancel)
        large = min(int(da.settings.large_node_bytes), int(db.settings.large_node_bytes))
        self.node_limit = max(1, min(VALUE_DIFF_MAX, large))
        self.value_limit = VALUE_DIFF_MAX
        self.progress = progress
        self._files: dict[int, Any] = {}
        self._steps = 0

    # -- reading ---------------------------------------------------------------------------
    def close(self) -> None:
        files, self._files = self._files, {}
        for f in files.values():
            try:
                f.close()
            except OSError:  # pragma: no cover
                pass

    def _file(self, doc: Any) -> Any:
        f = self._files.get(id(doc))
        if f is None:
            f = self._files[id(doc)] = doc.open_data()
        return f

    def _raw(self, doc: Any, node: Any) -> bytes:
        if node.is_composite:
            return b"".join(doc.node_chunks(node, cancel=self.cancel))
        return read_range(self._file(doc), node.start, node.end)

    def _value(self, doc: Any, node: Any) -> Any:
        return codec.loads(self._raw(doc, node))

    def value_or_omitted(self, doc: Any, node: Any) -> Any:
        """The node's value, or :class:`Omitted` above the value limit (or when it cannot be
        decoded in memory)."""
        if node.size <= self.value_limit:
            try:
                return self._value(doc, node)
            except _DECODE_ERRORS:
                pass
        return Omitted(node.kind, int(node.size), node.scalar_text(OMITTED_PREVIEW))

    def _raws(self, doc: Any, nodes: Sequence[Any]) -> list[bytes | None]:
        """The raw bytes of consecutive child nodes (``None`` above the value limit): neighbours
        in one address space are read together."""
        out: list[bytes | None] = [None] * len(nodes)
        limit = self.value_limit
        k = 0
        n = len(nodes)
        while k < n:
            c = nodes[k]
            if c.size > limit:
                k += 1
                continue
            if c.is_composite:
                out[k] = self._raw(doc, c)
                k += 1
                continue
            s0 = c.start
            e = c.end
            space = s0 >= EDIT_BASE
            j = k + 1
            while j < n:
                d = nodes[j]
                if d.is_composite or d.size > limit or (d.start >= EDIT_BASE) != space or d.start < e \
                        or d.end - s0 > WINDOW_READ_MAX:
                    break
                e = d.end
                j += 1
            buf = read_range(self._file(doc), s0, e)
            for q in range(k, j):
                out[q] = buf[nodes[q].start - s0:nodes[q].end - s0]
            k = j
        return out

    def _stream_hash(self, doc: Any, node: Any, compact: bool) -> bytes:
        """blake2b-128 of a node's text (compact form when ``compact``), streamed."""
        h = hashlib.blake2b(digest_size=16)
        fmt = core.Reformatter(None) if compact else None
        for chunk in doc.node_chunks(node, chunk=STREAM_CHUNK, cancel=self.cancel):
            h.update(fmt.feed(chunk) if fmt is not None else chunk)
        if fmt is not None:
            h.update(fmt.finish())
        return h.digest()

    def _children(self, node: Any, lo: int, hi: int) -> Iterator[list[Any]]:
        for s in range(lo, hi, CHILD_WINDOW):
            check(self.cancel)
            yield node.children(s, min(hi, s + CHILD_WINDOW))

    # -- driving ---------------------------------------------------------------------------
    def run(self, na: Any, nb: Any, pa: tuple = (), pb: tuple = ()) -> None:
        self._drive([(pa, pb, na, nb)])

    def _drive(self, stack: list[Any]) -> None:
        sink = self.sink
        while stack:
            item = stack.pop()
            cls = item.__class__
            if cls is Change:
                sink.emit(item)
                continue
            if cls is _Lazy:
                nxt = next(item.it, None)
                if nxt is not None:
                    stack.append(item)
                    stack.append(nxt)
                continue
            self._steps += 1
            if self._steps % 64 == 0:
                check(self.cancel)
            pa, pb, x, y = item
            self._pair(x, y, pa, pb, stack)

    def _pair(self, x: Any, y: Any, pa: tuple, pb: tuple, stack: list[Any]) -> None:
        if x.size <= self.node_limit and y.size <= self.node_limit:
            try:
                va = self._value(self.da, x)
                vb = self._value(self.db, y)
            except _DECODE_ERRORS:
                pass                       # not decodable in memory (e.g. too deep): structure below
            else:
                self.walker.run(va, vb, pa, pb)
                return
        kx, ky = x.kind, y.kind
        if kx != ky or kx not in ("object", "array"):
            if x.size <= self.value_limit and y.size <= self.value_limit:
                try:
                    va = self._value(self.da, x)
                    vb = self._value(self.db, y)
                except _DECODE_ERRORS:
                    pass
                else:
                    self.walker.run(va, vb, pa, pb)
                    return
            if kx == ky and x.size == y.size and \
                    self._stream_hash(self.da, x, False) == self._stream_hash(self.db, y, False):
                return                     # big scalars: the same raw text
            self.sink.add("changed", pa, pb, self.value_or_omitted(self.da, x), self.value_or_omitted(self.db, y))
            return
        it = self._objects(x, y, pa, pb) if kx == "object" else self._arrays(x, y, pa, pb)
        if self.progress is not None and not pa and not pb:
            it = self._ticking(it, x, y)       # the top level: progress by children done
        stack.append(_Lazy(it))

    def _ticking(self, it: Iterator[Any], x: Any, y: Any) -> Iterator[Any]:
        prog = self.progress
        assert prog is not None
        total = max(1, x.child_count(cancel=self.cancel) + y.child_count(cancel=self.cancel))
        if prog.total is None:
            prog.set_total(total)
        scale = prog.total / total
        done = 0
        for item in it:
            done += 1 if item.__class__ is Change else 2
            if done % 256 < 2:
                prog.update(int(min(total, done) * scale))
            yield item

    # -- objects ---------------------------------------------------------------------------
    def keys(self, node: Any) -> dict[str, int]:
        """Member key -> child index (dict semantics: the last duplicate wins, the order of
        the first occurrence)."""
        n = node.child_count(cancel=self.cancel)
        if n > OBJECT_KEYS_MAX:
            raise UserInputError("diff.error.too_many_keys", count=n, max=OBJECT_KEYS_MAX)
        d: dict[str, int] = {}
        i = 0
        for window in self._children(node, 0, n):
            for c in window:
                d[c.key] = i
                i += 1
        return d

    def _objects(self, x: Any, y: Any, pa: tuple, pb: tuple) -> Iterator[Any]:
        ka = self.keys(x)
        kb = self.keys(y)
        for k, i in ka.items():
            j = kb.get(k)
            cx = x.child(i)
            if j is None:
                yield Change("removed", pa + (k,), None, self.value_or_omitted(self.da, cx), MISSING)
            else:
                yield (pa + (k,), pb + (k,), cx, y.child(j))
        for k, j in kb.items():
            if k not in ka:
                yield Change("added", None, pb + (k,), MISSING, self.value_or_omitted(self.db, y.child(j)))

    # -- arrays ----------------------------------------------------------------------------
    def _compact_fps(self, doc: Any, node: Any, lo: int, hi: int) -> array:
        """Compact fingerprints of children ``lo .. hi-1`` (whitespace-insensitive identity)."""
        out = array("Q")
        for window in self._children(node, lo, hi):
            raws = self._raws(doc, window)
            for c, raw in zip(window, raws):
                out.append(_compact_fp(raw) if raw is not None else
                           int.from_bytes(self._stream_hash(doc, c, True)[:8], "little"))
        return out

    def _equal_ends(self, x: Any, y: Any, n: int, m: int) -> tuple[int, int]:
        """``(prefix, suffix)``: how many leading / trailing children of the two arrays have the
        same compact text -- read window by window from each end up to the first difference, so
        a child is either fingerprinted here or tokenized for the alignment, rarely both."""
        k = min(n, m)
        p = 0
        while p < k:
            w = min(CHILD_WINDOW, k - p)
            fa = self._compact_fps(self.da, x, p, p + w)
            fb = self._compact_fps(self.db, y, p, p + w)
            q = _common_prefix(fa, fb, 0, 0, w)
            p += q
            if q < w:
                break
        s = 0
        limit = k - p
        while s < limit:
            w = min(CHILD_WINDOW, limit - s)
            fa = self._compact_fps(self.da, x, n - s - w, n - s)
            fb = self._compact_fps(self.db, y, m - s - w, m - s)
            q = _common_suffix(fa, fb, w, w, w)
            s += q
            if q < w:
                break
        return p, s

    def _tokens(self, doc: Any, node: Any, lo: int, hi: int) -> array:
        """Token hashes of children ``lo .. hi-1`` (the in-memory tokens' identity; 8 bytes per
        child)."""
        out = array("Q")
        text = self.text
        for window in self._children(node, lo, hi):
            raws = self._raws(doc, window)
            for c, raw in zip(window, raws):
                tok: bytes | None = None
                if raw is not None:
                    try:
                        tok = _token(codec.loads(raw), text)
                    except _DECODE_ERRORS:
                        tok = None
                if tok is None:
                    tok = b"\x04" + self._stream_hash(doc, c, True)
                out.append(_hash64(tok))
        return out

    def _child_keys(self, doc: Any, node: Any, n: int, fpath: tuple) -> list[bytes] | None:
        out: list[bytes] = []
        for window in self._children(node, 0, n):
            raws = self._raws(doc, window)
            for raw in raws:
                if raw is None:
                    return None
                try:
                    v = codec.loads(raw)
                except _DECODE_ERRORS:
                    return None
                keys = _element_keys((v,), fpath)
                if keys is None:
                    return None
                out.append(keys[0])
        return out

    def _arrays(self, x: Any, y: Any, pa: tuple, pb: tuple) -> Iterator[Any]:
        n = x.child_count(cancel=self.cancel)
        m = y.child_count(cancel=self.cancel)
        method, fpath = self.walker.method(pa)
        if method in ("key", "unordered"):
            if n + m <= 2 * PATIENCE_MAX:
                fp = fpath if method == "key" else ()
                ka = self._child_keys(self.da, x, n, fp)       # type: ignore[arg-type]
                kb = self._child_keys(self.db, y, m, fp) if ka is not None else None   # type: ignore[arg-type]
                if ka is not None and kb is not None:
                    yield from self._keyed(x, y, pa, pb, ka, kb, moves=method == "key")
                    return
            method = "auto"
        if method == "index" or (n == 1 and m == 1):
            yield from self._index_pairs(x, y, pa, pb, n, m)
            return
        p, s = self._equal_ends(x, y, n, m)
        ta = self._tokens(self.da, x, p, n - s)
        tb = self._tokens(self.db, y, p, m - s)
        ops = [_shift(op, p) for op in _align(ta, tb, self.cancel)]
        del ta, tb
        for op in ops:
            yield from self._op_items(op, x, y, pa, pb)

    def _op_items(self, op: tuple, x: Any, y: Any, pa: tuple, pb: tuple) -> Iterator[Any]:
        tag = op[0]
        if tag == _EQ:
            return
        if tag == _PAIRS:
            _, i, j, n = op
            for off in range(0, n, CHILD_WINDOW):
                k = min(CHILD_WINDOW, n - off)
                ca = x.children(i + off, i + off + k)
                cb = y.children(j + off, j + off + k)
                for q in range(k):
                    yield (pa + (i + off + q,), pb + (j + off + q,), ca[q], cb[q])
        elif tag == _DEL:
            _, i, n = op
            for window in self._children(x, i, i + n):
                for c in window:
                    yield Change("removed", pa + (int(c.key),), None, self.value_or_omitted(self.da, c), MISSING)
        else:
            _, j, n = op
            for window in self._children(y, j, j + n):
                for c in window:
                    yield Change("added", None, pb + (int(c.key),), MISSING, self.value_or_omitted(self.db, c))

    def _index_pairs(self, x: Any, y: Any, pa: tuple, pb: tuple, n: int, m: int) -> Iterator[Any]:
        k = min(n, m)
        for off in range(0, k, CHILD_WINDOW):
            q = min(CHILD_WINDOW, k - off)
            ca = x.children(off, off + q)
            cb = y.children(off, off + q)
            ra = self._raws(self.da, ca)
            rb = self._raws(self.db, cb)
            for r in range(q):
                if ra[r] is not None and rb[r] is not None and _compact_fp(ra[r]) == _compact_fp(rb[r]):
                    continue           # the same compact text: equal
                yield (pa + (off + r,), pb + (off + r,), ca[r], cb[r])
        if n > k:
            yield from self._op_items((_DEL, k, n - k), x, y, pa, pb)
        if m > k:
            yield from self._op_items((_INS, k, m - k), x, y, pa, pb)

    def _keyed(self, x: Any, y: Any, pa: tuple, pb: tuple, ka: list[bytes], kb: list[bytes],
               moves: bool = True) -> Iterator[Any]:
        pairs, keep = _key_match(ka, kb)
        pair_of = {i: (k, j) for k, (i, j) in enumerate(pairs)}
        matched_b = {j for _, j in pairs}
        n = len(ka)
        for window in self._children(x, 0, n):
            for c in window:
                i = int(c.key)
                hit = pair_of.get(i)
                if hit is None:
                    yield Change("removed", pa + (i,), None, self.value_or_omitted(self.da, c), MISSING)
                    continue
                k, j = hit
                if moves and k not in keep:
                    yield Change("moved", pa + (i,), pb + (j,))
                yield (pa + (i,), pb + (j,), c, y.child(j))
        for j in range(len(kb)):
            if j not in matched_b:
                yield Change("added", None, pb + (j,), MISSING, self.value_or_omitted(self.db, y.child(j)))

    # -- member records compared as {key: value} ------------------------------------------
    def run_members(self, na: Any, nb: Any, wrap_a: bool, wrap_b: bool) -> None:
        """Record nodes of member records (the VALUE nodes, ``Node.key`` the member key)
        compared as one-member objects on the wrapped side(s)."""
        if wrap_a and wrap_b:
            if na.key == nb.key:
                self.run(na, nb, (na.key,), (nb.key,))
            else:
                self.sink.add("removed", (na.key,), None, self.value_or_omitted(self.da, na), MISSING)
                self.sink.add("added", None, (nb.key,), MISSING, self.value_or_omitted(self.db, nb))
            return
        if wrap_a:
            if nb.kind != "object":
                self.sink.add("changed", (), (), _wrap_member(na.key, self.value_or_omitted(self.da, na)),
                              self.value_or_omitted(self.db, nb))
                return
            kb = self.keys(nb)
            stack: list[Any] = []
            items: list[Any] = []
            j = kb.get(na.key)
            if j is None:
                items.append(Change("removed", (na.key,), None, self.value_or_omitted(self.da, na), MISSING))
            else:
                items.append(((na.key,), (na.key,), na, nb.child(j)))
            for k, jj in kb.items():
                if k != na.key:
                    items.append(Change("added", None, (k,), MISSING, self.value_or_omitted(self.db, nb.child(jj))))
            items.reverse()
            stack.extend(items)
            self._drive(stack)
            return
        if na.kind != "object":
            self.sink.add("changed", (), (), self.value_or_omitted(self.da, na),
                          _wrap_member(nb.key, self.value_or_omitted(self.db, nb)))
            return
        ka = self.keys(na)
        items = []
        for k, i in ka.items():
            if k == nb.key:
                items.append(((k,), (k,), na.child(i), nb))
            else:
                items.append(Change("removed", (k,), None, self.value_or_omitted(self.da, na.child(i)), MISSING))
        if nb.key not in ka:
            items.append(Change("added", None, (nb.key,), MISSING, self.value_or_omitted(self.db, nb)))
        items.reverse()
        self._drive(items)


def _wrap_member(key: str, value: Any) -> Any:
    """The one-member object ``{key: value}`` of a member record; a value too large to hold makes
    the whole member an :class:`Omitted` object (never an Omitted inside a value)."""
    if isinstance(value, Omitted):
        preview = f"{{{codec.dumps(key).decode('utf-8', 'replace')}: {value.preview}"
        if len(preview) > OMITTED_PREVIEW:
            preview = preview[:OMITTED_PREVIEW - 1] + _ELLIPSIS
        return Omitted("object", value.size + len(key.encode("utf-8", "surrogatepass")) + 5, preview)
    return {key: value}


def _shift(op: tuple, p: int) -> tuple:
    tag = op[0]
    if tag in (_EQ, _PAIRS):
        return (tag, op[1] + p, op[2] + p, op[3])
    return (tag, op[1] + p, op[2])


def diff_nodes(doc_a: Any, node_a: Any, doc_b: Any, node_b: Any, opts: DiffOptions | None = None, *,
               cancel: CancelToken | None = None, progress: Progress | None = None) -> ValueDiff:
    """The differences between two nodes (``doc_a.node(...)`` / ``doc_a.root()`` ...; the
    documents they belong to are given for reading) without loading big nodes: nodes up to
    ``min(VALUE_DIFF_MAX, Settings.large_node_bytes)`` per side are parsed and walked, bigger
    ones are traversed through the Node API (module docstring). Paths are relative to the two
    nodes. Raises ``Cancelled``, ``UserInputError("diff.error.too_many_keys")``."""
    opts = _opts(opts)
    sink = _Sink(opts.max_changes)
    nd = _NodeDiff(doc_a, doc_b, opts, sink, cancel, progress=progress)
    try:
        nd.run(node_a, node_b)
    finally:
        nd.close()
    if progress is not None:
        progress.finish()
    return sink.vd


# ----------------------------------------------------------------------------------
# file diffs: helpers
# ----------------------------------------------------------------------------------
class _Temp:
    """A private temporary directory ``<base>/sort-<pid>-<hex>`` (the sort runs' naming, so
    ``recordset.purge_stale_files`` removes it after a crash), created on first use."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.dir: Path | None = None

    def path(self, name: str) -> Path:
        if self.dir is None:
            for _ in range(100):
                d = self.base / f"sort-{os.getpid()}-{secrets.token_hex(4)}"
                try:
                    os.mkdir(long_path(d))
                    break
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise os_error(exc, d, writing=True) from exc
            else:  # pragma: no cover - 100 name collisions
                raise JsonReshapeError("recordset.error.write", path=str(self.base), detail="no temporary name")
            self.dir = d
        return self.dir / name

    def cleanup(self) -> None:
        d, self.dir = self.dir, None
        if d is not None:
            _remove_tree(d)


class _FpStore:
    """Record fingerprints by position: an ``array('Q')`` up to ``memory_entries``, beyond it a
    temporary file read through a memory map."""

    def __init__(self, temp: _Temp, name: str, memory_entries: int | None = None) -> None:
        self.temp = temp
        self.name = name
        self.limit = max(1, int(FP_MEMORY_ENTRIES if memory_entries is None else memory_entries))
        self.mem = array("Q")
        self.count = 0
        self._f: Any = None
        self._path: Path | None = None
        self._mm: Any = None
        self._view: Any = None

    def extend(self, values: array) -> None:
        if self._f is None and len(self.mem) + len(values) <= self.limit:
            self.mem.extend(values)
        else:
            try:
                if self._f is None:
                    self._path = self.temp.path(self.name)
                    self._f = open(long_path(self._path), "wb", buffering=1 << 20)  # noqa: SIM115 - closed later
                    self._f.write(self.mem.tobytes())
                    self.mem = array("Q")
                self._f.write(values.tobytes())
            except OSError as exc:
                raise os_error(exc, self._path or self.temp.base, writing=True) from exc
        self.count += len(values)

    def finish(self) -> _FpStore:
        if self._f is not None:
            f, self._f = self._f, None
            try:
                f.close()
                if self.count:
                    with open(long_path(self._path), "rb") as fh:   # type: ignore[arg-type]
                        self._mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
                    self._view = memoryview(self._mm).cast("Q")
            except OSError as exc:
                raise os_error(exc, self._path or self.temp.base) from exc
        return self

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, i: int) -> int:
        view = self._view
        return view[i] if view is not None else self.mem[i]

    def close(self) -> None:
        view, self._view = self._view, None
        if view is not None:
            view.release()
        mm, self._mm = self._mm, None
        if mm is not None:
            mm.close()
        f, self._f = self._f, None
        if f is not None:
            try:
                f.close()
            except OSError:  # pragma: no cover
                pass
        self.mem = array("Q")


def _bit_positions(bits: bytearray) -> Iterator[int]:
    offsets = _BIT_OFFSETS
    for m in _NONZERO.finditer(bits):
        for b in range(m.start(), m.end()):
            base = b << 3
            for off in offsets[bits[b]]:
                yield base + off


class _Meter:
    """Byte progress of a file diff: fractions of the bar (index builds, passes, matching,
    comparing) mapped onto ``progress`` (total = bytes of A + B, unless the caller set one)."""

    def __init__(self, progress: Progress | None, message: str) -> None:
        self.p = progress
        if progress is not None and not progress.message:
            progress.set_message(message)
        self.best = 0.0

    def set_total(self, total: int) -> None:
        if self.p is not None and self.p.total is None:
            self.p.set_total(max(1, int(total)))

    def child(self, lo: float, hi: float, total: int | None) -> Progress | None:
        if self.p is None or not self.p.total:
            return None
        return self.p.child(lo, hi, total=max(1, int(total)) if total is not None else None)

    def at(self, fraction: float) -> None:
        p = self.p
        if p is None or not p.total or fraction <= self.best:
            return
        self.best = min(1.0, fraction)
        p.update(int(p.total * self.best))

    def message(self, text: str) -> None:
        if self.p is not None:
            self.p.set_message(text)

    def finish(self) -> None:
        if self.p is not None:
            self.p.finish()


def _sources_of(*docs: Any) -> tuple[Path, ...]:
    """The files the documents read -- each one's source and data path (a prepared copy of a gzip /
    UTF-16 source) --, each file once (``DiffReport.sources``)."""
    out: dict[str, Path] = {}
    for doc in docs:
        for p in (getattr(doc, "source_path", None), getattr(doc, "data_path", None)):
            if p is not None:
                out.setdefault(normalize_path(p), Path(p))
    return tuple(out.values())


def _ensure_unchanged(doc: Any) -> None:
    ensure = getattr(doc, "ensure_unchanged", None)
    if ensure is not None:
        ensure()


PASS_END = 0.8        # the passes over the records fill the bar up to here (key / align modes)
MATCH_END = 0.85      # the matching (merge-join / alignment) up to here; the comparisons do the rest


class _Run:
    """One file diff (``iter_record_diffs``): argument checks at construction, the work in
    :meth:`records` (a generator returning the :class:`DiffReport`)."""

    def __init__(self, doc_a: Any, doc_b: Any, *, key: str | None, mode: str, opts: DiffOptions | None,
                 max_details: int | None, memory_limit: int | None, results: bool, progress: Progress | None,
                 cancel: CancelToken | None, strategy: str, block_bytes: int) -> None:
        mode_ = mode.strip().lower() if isinstance(mode, str) else None
        if mode_ not in MODES:
            raise UserInputError("diff.error.mode", value=repr(mode))
        self.opts = _opts(opts)
        if max_details is not None and (isinstance(max_details, bool) or not isinstance(max_details, int)
                                        or max_details < 0):
            raise UserInputError("diff.error.max_details", value=repr(max_details))
        if memory_limit is not None and (isinstance(memory_limit, bool) or not isinstance(memory_limit, int)
                                         or memory_limit < 1):
            raise UserInputError("diff.error.memory_limit", value=repr(memory_limit))
        self.strategy = _check_strategy(strategy)
        self.key_text: str | None = None
        self.key_path: tuple | None = None
        if key is not None:
            if mode_ not in ("auto", "key"):
                raise UserInputError("diff.error.key_mode", mode=mode_)
            self.key_text, self.key_path = parse_key(key)
            mode_ = "key"
        elif mode_ == "key":
            raise UserInputError("diff.error.key_required")
        self.mode = mode_
        self.max_details = max_details
        self.memory_limit = memory_limit
        self.results = bool(results)
        self.progress = progress
        self.cancel = cancel
        self.block_bytes = max(1, int(block_bytes))
        self.a = doc_a.pinned()
        self.b = doc_b.pinned()
        self.settings = getattr(self.a, "settings", None) or get_settings()
        self.report = DiffReport(mode=self.mode, key=self.key_text, options=self.opts,
                                 a_name=self.a.source_path.name, b_name=self.b.source_path.name,
                                 sources=_sources_of(self.a, self.b))
        self._cleanups: list[Callable[[], None]] = []
        self._res_temp: _Temp | None = None
        self._res_file: Any = None
        self.value_only = False
        self.wrap_a = self.wrap_b = False
        self.hashers: dict[str, KeyHasher] = {}
        self.meter: _Meter | None = None
        self.start = 0.0
        self.size_a = self.size_b = 0
        self.value_limit = VALUE_DIFF_MAX
        self.out_lo = MATCH_END
        self.out_total = 1
        self.out_done = 0

    # -- bookkeeping ---------------------------------------------------------------------------
    def _warn(self, message_key: str, /, **params: Any) -> None:
        self.report.warnings.append(message_key)
        self.report.warning_params.append(params)

    def _details_left(self) -> bool:
        return self.max_details is None or self.report.details < self.max_details

    def _account(self, rd: RecordDiff) -> None:
        rep = self.report
        if rd.status == "changed":
            rep.changed += 1
        elif rd.status == "added":
            rep.added += 1
        else:
            rep.removed += 1
        if rd.diff is not None:
            rep.details += 1
            rep.moved += rd.diff.counts.get("moved", 0)
        f = self._res_file
        if f is not None:
            try:
                f.write(RESULT_ENTRY.pack(STATUS_CODES[rd.status], NO_INDEX if rd.a_index is None else rd.a_index,
                                          NO_INDEX if rd.b_index is None else rd.b_index))
            except OSError as exc:
                raise os_error(exc, self.report.results_file or "", writing=True) from exc
            rep.results_count += 1

    def _cleanup(self) -> None:
        for fn in reversed(self._cleanups):
            try:
                fn()
            except Exception:  # pragma: no cover - best effort
                log.debug("diff cleanup failed", exc_info=True)
        self._cleanups = []

    def _drop_results(self) -> None:
        f, self._res_file = self._res_file, None
        if f is not None:
            try:
                f.close()
            except OSError:  # pragma: no cover
                pass
        temp, self._res_temp = self._res_temp, None
        if temp is not None:
            temp.cleanup()
        self.report.results_file = None
        self.report._temp = None

    # -- the run ---------------------------------------------------------------------------------
    def records(self) -> Generator[RecordDiff, None, DiffReport]:
        started = time.perf_counter()
        try:
            wrapper = self._prepare()
            if wrapper is not None:
                yield wrapper
            if self.mode == "key":
                yield from self._key_mode()
            elif self.mode == "align":
                yield from self._align_mode()
            elif self.mode == "position":
                yield from self._position_mode()
            else:
                yield from self._document_mode()
            check(self.cancel)
            _ensure_unchanged(self.a)
            _ensure_unchanged(self.b)
            rep = self.report
            f, self._res_file = self._res_file, None
            if f is not None:
                try:
                    f.close()
                except OSError as exc:
                    raise os_error(exc, rep.results_file or "", writing=True) from exc
            rep.elapsed = time.perf_counter() - started
            if self.meter is not None:
                self.meter.finish()
            log.info("diff of %s and %s (%s%s): %d equal, %d changed, %d added, %d removed (%.2f s)", rep.a_name,
                     rep.b_name, rep.mode, f" by {rep.key}" if rep.key else "", rep.equal, rep.changed, rep.added,
                     rep.removed, rep.elapsed)
            return rep
        except BaseException:
            self._drop_results()
            raise
        finally:
            self._cleanup()

    def _prepare(self) -> RecordDiff | None:
        a, b, rep = self.a, self.b, self.report
        self.meter = meter = _Meter(self.progress, t("diff.progress.scan", a=rep.a_name, b=rep.b_name))
        todo = [d for d in (a, b) if not d.index_complete]
        if todo:
            meter.set_total(sum(max(1, int(d.data_size)) for d in (a, b)))
            for k, d in enumerate(todo):
                d.build_index(progress=meter.child(0.25 * k / len(todo), 0.25 * (k + 1) / len(todo),
                                                   max(1, int(d.data_size))), cancel=self.cancel)
            meter.at(0.25)
        check(self.cancel)
        self.start = meter.best
        self.size_a = _data_bytes(a)
        self.size_b = _data_bytes(b)
        meter.set_total(self.size_a + self.size_b)
        rep.a_records = int(a.record_count)
        rep.b_records = int(b.record_count)
        rep.a_gen = getattr(a, "gen", None)
        rep.b_gen = getattr(b, "gen", None)
        large = min(int(a.settings.large_node_bytes), int(b.settings.large_node_bytes))
        self.value_limit = max(1, min(VALUE_DIFF_MAX, large))
        # -- the mode --------------------------------------------------------------------------
        if self.mode == "auto":
            if a.member_records and b.member_records:
                self.mode = "key"
                self.key_text, self.key_path = KEY_FIELD, None
            elif a.kind == "single" and b.kind == "single":
                self.mode = "document"
            else:
                self.mode = "align"
        if self.mode == "align":
            limit = int(self.settings.diff_align_max_bytes)
            big = max(self.size_a, self.size_b)
            if big > limit:
                raise UserInputError("diff.error.too_big_no_key", size=format_bytes(big), limit=format_bytes(limit))
        rep.mode = self.mode
        rep.key = self.key_text
        self.value_only = self.mode == "key" and self.key_path is None
        self.wrap_a = bool(a.member_records) and not self.value_only
        self.wrap_b = bool(b.member_records) and not self.value_only
        # -- notes on the inputs ---------------------------------------------------------------
        for d, name in ((a, rep.a_name), (b, rep.b_name)):
            if getattr(d, "error", None) is not None:
                self._warn("diff.warn.partial", name=name, count=int(d.record_count))
            broken = int(getattr(d, "broken_count", 0) or 0)
            if broken:
                self._warn("diff.warn.broken_lines", name=name, count=broken)
        if self.results:
            self._res_temp = _Temp(work_dir(self.settings.cache_dir))
            path = self._res_temp.path("diff-results.bin")
            try:
                self._res_file = open(long_path(path), "wb", buffering=1 << 16)  # noqa: SIM115 - closed in records()
            except OSError as exc:
                raise os_error(exc, path, writing=True) from exc
            rep.results_file = path
            rep._temp = self._res_temp
        return self._wrapper_diff() if self.mode != "document" else None

    # -- one pair (record_diff) ----------------------------------------------------------------------
    def one(self, pa: int | None, pb: int | None) -> RecordDiff:
        """The details of one record pair (or one lone record) with the settings of a file diff in
        this run's mode (``key`` / ``position``)."""
        a, b = self.a, self.b
        for d in (a, b):
            if not d.index_complete:
                d.build_index(cancel=self.cancel)
        large = min(int(a.settings.large_node_bytes), int(b.settings.large_node_bytes))
        self.value_limit = max(1, min(VALUE_DIFF_MAX, large))
        self.value_only = self.mode == "key" and self.key_path is None
        self.wrap_a = bool(a.member_records) and not self.value_only
        self.wrap_b = bool(b.member_records) and not self.value_only
        for p, d in ((pa, a), (pb, b)):
            if p is not None and (isinstance(p, bool) or not isinstance(p, int) or not 0 <= p < int(d.record_count)):
                raise IndexError(f"record {p!r} out of range ({int(d.record_count)} records)")
        if self.mode == "key" and self.key_path is not None:
            base = work_dir(self.settings.cache_dir)
            for side, d in (("a", a), ("b", b)):
                runs = HashRuns(base, self.cancel)          # (nothing is spilled: values are only streamed)
                self._cleanups.append(runs.cleanup)
                self.hashers[side] = KeyHasher(d, self.key_path, self.strategy, d.settings, self.cancel,
                                               _dupes.MIN_RUN_ENTRIES, runs)
        raw_a = self._read_raw(a, [pa]).get(pa, MISSING) if pa is not None else MISSING
        raw_b = self._read_raw(b, [pb]).get(pb, MISSING) if pb is not None else MISSING
        if pa is not None and pb is not None:
            rd = self._pair(pa, pb, raw_a, raw_b)
            return rd if rd is not None else RecordDiff("changed", pa, pb, MISSING, ValueDiff())
        if pa is not None:
            return self._lone("removed", pa, raw_a)
        return self._lone("added", pb, raw_b)    # type: ignore[arg-type]

    # -- wrappers ----------------------------------------------------------------------------------
    def _wrapper_value(self, doc: Any) -> Any:
        spans = doc.wrapper_spans(cancel=self.cancel)
        if spans is None:
            return MISSING
        (s1, e1), (s2, e2) = spans
        if (e1 - s1) + (e2 - s2) > VALUE_DIFF_MAX:
            raise _TooLargeWrapper((e1 - s1) + (e2 - s2))
        with doc.open_data() as f:
            text = read_range(f, s1, e1) + b"null" + read_range(f, s2, e2)
        return codec.loads(text)

    def _wrapper_diff(self) -> RecordDiff | None:
        a, b, rep = self.a, self.b, self.report
        wa = a.kind == "wrapped"
        wb = b.kind == "wrapped"
        if wa and wb:
            try:
                va = self._wrapper_value(a)
                vb = self._wrapper_value(b)
            except _TooLargeWrapper as exc:
                self._warn("diff.warn.wrapper_too_large", size=format_bytes(exc.size))
                return None
            if va is MISSING or vb is MISSING:
                return None
            sink = _Sink(self.opts.max_changes)
            _Walker(self.opts, sink, 0, self.cancel).run(va, vb)
            if sink.vd.equal:
                return None
            rep.wrapper = sink.vd
            return RecordDiff("changed", None, None, MISSING, sink.vd, scope="wrapper")
        if wa or wb:
            self._warn("diff.warn.wrapper_one_side", name=rep.a_name if wa else rep.b_name)
        return None

    # -- passes over the records ------------------------------------------------------------------
    def _block_fps(self, doc: Any, blk: Any, value_only: bool) -> array:
        idx = blk.indices
        member_values = value_only and bool(doc.member_records)
        if blk.huge:
            pos = int(idx[0])
            s, e = doc.record_value_span(pos) if member_values else blk.span(0)
            return array("Q", [self._stream_fp(doc, s, e)])
        data = blk.data
        sp = _u64(blk.spans)
        ref = core.Reformatter
        blake = hashlib.blake2b
        digests: list[bytes] = []
        put = digests.append
        cancel = self.cancel
        for k in range(len(idx)):
            if k % CHECK_EVERY == 0:
                check(cancel)
            raw = data[sp[2 * k]:sp[2 * k + 1]]
            if member_values:
                raw = raw[split_member(raw)[1]:]
            f = ref(None)
            put(blake(f.feed(raw) + f.finish(), digest_size=8).digest())
        out = array("Q")
        out.frombytes(b"".join(digests))       # the digests as native u64 (_stream_fp reads them the same way)
        return out

    def _stream_fp(self, doc: Any, s: int, e: int) -> int:
        f = core.Reformatter(None)
        h = hashlib.blake2b(digest_size=8)
        with doc.open_data() as fh:
            pos = s
            while pos < e:
                check(self.cancel)
                stop = min(e, pos + STREAM_CHUNK)
                h.update(f.feed(read_range(fh, pos, stop)))
                pos = stop
        h.update(f.finish())
        out = array("Q")
        out.frombytes(h.digest())
        return out[0]

    def _pass(self, doc: Any, store: Any, hasher: KeyHasher | None, value_only: bool, prog: Progress | None) -> None:
        """One pass over every record: the key entries (``hasher``) and the fingerprints."""
        blocks = doc.iter_blocks(None, block_bytes=self.block_bytes, progress=prog, cancel=self.cancel)
        rep = self.report
        try:
            for blk in blocks:
                check(self.cancel)
                if len(blk.indices) and int(blk.indices[0]) != len(store):  # pragma: no cover - would be a bug
                    raise RuntimeError("diff: blocks out of position order")
                if hasher is not None:
                    hasher.block(blk)
                store.extend(self._block_fps(doc, blk, value_only))
                rep.bytes_read += (blk.span(0)[1] - blk.span(0)[0]) if blk.huge else len(blk.data)
        finally:
            blocks.close()
        if prog is not None:
            prog.finish()

    def _pass_children(self) -> tuple[Progress | None, Progress | None]:
        meter = self.meter
        assert meter is not None
        lo = self.start
        whole = max(1, self.size_a + self.size_b)
        mid = lo + (PASS_END - lo) * self.size_a / whole
        return meter.child(lo, mid, self.size_a), meter.child(mid, PASS_END, self.size_b)

    # -- key mode ----------------------------------------------------------------------------------
    def _key_mode(self) -> Iterator[RecordDiff]:
        a, b, rep = self.a, self.b, self.report
        cancel = self.cancel
        path = self.key_path
        total = int(self.settings.memory_limit_bytes)
        budget = min(self.memory_limit or max(1, total // 4), max(1, total // 2))   # as dupes._memory_limit
        # the entries of A and B are held together until the merge-join, which frees them batch by
        # batch (dupes.HashRuns.merged) while the changed pairs accumulate: half the budget per side
        max_entries = max(_dupes.MIN_RUN_ENTRIES, budget // ENTRY_BYTES // 2)     # (read now: tests lower it)
        base = work_dir(self.settings.cache_dir)
        runs_a, runs_b, runs_c = HashRuns(base, cancel), HashRuns(base, cancel), HashRuns(base, cancel)
        temp = _Temp(base)
        fa = _FpStore(temp, "fp-a.bin")
        fb = _FpStore(temp, "fp-b.bin")
        self._cleanups += [runs_a.cleanup, runs_b.cleanup, runs_c.cleanup, temp.cleanup, fb.close, fa.close]
        ha = KeyHasher(a, path, self.strategy, a.settings, cancel, max_entries, runs_a)
        hb = KeyHasher(b, path, self.strategy, b.settings, cancel, max_entries, runs_b)
        self.hashers = {"a": ha, "b": hb}
        prog_a, prog_b = self._pass_children()
        self._pass(a, fa, ha, self.value_only, prog_a)
        self._pass(b, fb, hb, self.value_only, prog_b)
        fa.finish()
        fb.finish()
        check(cancel)
        _ensure_unchanged(a)
        _ensure_unchanged(b)
        self._key_warnings(ha, hb)
        # -- merge-join ----------------------------------------------------------------------------
        meter = self.meter
        assert meter is not None
        meter.message(t("diff.progress.match", a=rep.a_name, b=rep.b_name))
        meter.at(PASS_END)
        na, nb = len(fa), len(fb)
        rem = bytearray((na + 7) >> 3)
        add = bytearray((nb + 7) >> 3)
        entries_a, ha.entries = ha.entries, []
        entries_b, hb.entries = hb.entries, []
        total = max(1, ha.with_key + hb.with_key)
        sa = runs_a.merged(entries_a)
        sb = runs_b.merged(entries_b)
        changed: list[int] = []
        n_changed = 0
        mask = _POS_MASK
        dup_a = dup_b = 0
        prev_a = prev_b = -1
        counted_a = counted_b = False
        equal = 0
        steps = 0
        try:
            ea = next(sa, None)
            eb = next(sb, None)
            ka = ea >> 64 if ea is not None else _INF_KEY
            kb = eb >> 64 if eb is not None else _INF_KEY
            while ea is not None or eb is not None:
                steps += 1
                if steps % CHECK_EVERY == 0:
                    check(cancel)
                    meter.at(PASS_END + (MATCH_END - PASS_END) * steps / total)
                if ka <= kb:
                    if ka == prev_a:
                        if not counted_a:
                            dup_a += 1
                            counted_a = True
                    else:
                        prev_a = ka
                        counted_a = False
                if kb <= ka:
                    if kb == prev_b:
                        if not counted_b:
                            dup_b += 1
                            counted_b = True
                    else:
                        prev_b = kb
                        counted_b = False
                if ka < kb:
                    pa = ea & mask                         # type: ignore[operator]
                    rem[pa >> 3] |= 1 << (pa & 7)
                    ea = next(sa, None)
                    ka = ea >> 64 if ea is not None else _INF_KEY
                elif kb < ka:
                    pb = eb & mask                         # type: ignore[operator]
                    add[pb >> 3] |= 1 << (pb & 7)
                    eb = next(sb, None)
                    kb = eb >> 64 if eb is not None else _INF_KEY
                else:
                    pa = ea & mask                         # type: ignore[operator]
                    pb = eb & mask                         # type: ignore[operator]
                    if fa[pa] == fb[pb]:
                        equal += 1
                    else:
                        changed.append((pa << 64) | pb)
                        n_changed += 1
                        if len(changed) >= max_entries:
                            runs_c.spill(changed)
                            changed = []
                    ea = next(sa, None)
                    ka = ea >> 64 if ea is not None else _INF_KEY
                    eb = next(sb, None)
                    kb = eb >> 64 if eb is not None else _INF_KEY
        finally:
            sa.close()
            sb.close()
        rep.equal += equal
        rep.duplicate_keys = (dup_a, dup_b)
        for n, name in ((dup_a, rep.a_name), (dup_b, rep.b_name)):
            if n:
                self._warn("diff.warn.duplicate_keys", name=name, count=n, key=self.key_text)
        # -- the differing records: changed + removed in A order, then the added -------------------
        self.out_total = max(1, n_changed + _popcount(rem) + _popcount(add))
        meter.message(t("diff.progress.compare", a=rep.a_name, b=rep.b_name))
        meter.at(MATCH_END)

        def items() -> Iterator[tuple[int | None, int | None]]:
            cs = runs_c.merged(changed)
            try:
                pairs = ((e >> 64, e & mask) for e in cs)
                removed = ((p, None) for p in _bit_positions(rem))
                yield from heapq.merge(pairs, removed)     # type: ignore[misc]
            finally:
                cs.close()
            for p in _bit_positions(add):
                yield None, p

        yield from self._emit(items())

    def _key_warnings(self, ha: KeyHasher, hb: KeyHasher) -> None:
        rep = self.report
        rep.missing_keys = (ha.missing, hb.missing)
        rep.skipped_keys = (ha.too_large + ha.unreadable, hb.too_large + hb.unreadable)
        for h, name, d in ((ha, rep.a_name, self.a), (hb, rep.b_name, self.b)):
            if self.key_path is None and not d.member_records:
                if h.missing:
                    self._warn("diff.warn.no_record_keys", name=name, count=h.missing)
            elif h.missing:
                self._warn("diff.warn.missing_keys", name=name, count=h.missing, key=self.key_text)
            if h.too_large:
                self._warn("diff.warn.key_too_large", name=name, count=h.too_large, record=h.first_too_large,
                           size=format_bytes(h.budget))
            if h.unreadable:
                self._warn("diff.warn.key_unreadable", name=name, count=h.unreadable, record=h.first_unreadable)

    # -- align mode --------------------------------------------------------------------------------
    def _align_mode(self) -> Iterator[RecordDiff]:
        a, b, rep = self.a, self.b, self.report
        na, nb = int(a.record_count), int(b.record_count)
        fa = array("Q")
        fb = array("Q")
        prog_a, prog_b = self._pass_children()
        self._pass(a, fa, None, False, prog_a)
        self._pass(b, fb, None, False, prog_b)
        if len(fa) != na or len(fb) != nb:  # pragma: no cover - would be a bug
            raise RuntimeError("diff: fingerprint count mismatch")
        check(self.cancel)
        meter = self.meter
        assert meter is not None
        meter.message(t("diff.progress.match", a=rep.a_name, b=rep.b_name))
        meter.at(PASS_END)
        ops = _align(fa, fb, self.cancel)
        self.out_total = max(1, sum(op[3] if op[0] == _PAIRS else op[2] for op in ops if op[0] != _EQ))
        meter.message(t("diff.progress.compare", a=rep.a_name, b=rep.b_name))
        meter.at(MATCH_END)

        def items() -> Iterator[tuple[int | None, int | None]]:
            for op in ops:
                tag = op[0]
                if tag == _EQ:
                    rep.equal += op[3]
                elif tag == _PAIRS:
                    _, i, j, n = op
                    for k in range(n):
                        if fa[i + k] == fb[j + k]:
                            rep.equal += 1
                        else:
                            yield i + k, j + k
                elif tag == _DEL:
                    for k in range(op[2]):
                        yield op[1] + k, None
                else:
                    for k in range(op[2]):
                        yield None, op[1] + k

        yield from self._emit(items())

    # -- position mode -----------------------------------------------------------------------------
    def _records(self, doc: Any, side: int, counter: list[int]) -> Iterator[tuple[int, bytes | None, int]]:
        """``(position, raw bytes or None above the value limit, fingerprint)`` of every record."""
        blocks = doc.iter_blocks(None, block_bytes=self.block_bytes, cancel=self.cancel)
        limit = self.value_limit
        meter = self.meter
        whole = max(1, self.size_a + self.size_b)
        lo = self.start
        try:
            for blk in blocks:
                check(self.cancel)
                fps = self._block_fps(doc, blk, False)
                if blk.huge:
                    s, e = blk.span(0)
                    counter[side] += e - s
                    if meter is not None:
                        meter.at(lo + (1.0 - lo) * 0.98 * min(1.0, (counter[0] + counter[1]) / whole))
                    yield int(blk.indices[0]), None, fps[0]
                    continue
                counter[side] += len(blk.data)
                if meter is not None:
                    meter.at(lo + (1.0 - lo) * 0.98 * min(1.0, (counter[0] + counter[1]) / whole))
                data = blk.data
                sp = _u64(blk.spans)
                idx = blk.indices
                for k in range(len(idx)):
                    s, e = sp[2 * k], sp[2 * k + 1]
                    yield int(idx[k]), (data[s:e] if e - s <= limit else None), fps[k]
        finally:
            blocks.close()

    def _position_mode(self) -> Iterator[RecordDiff]:
        rep = self.report
        meter = self.meter
        assert meter is not None
        counter = [0, 0]
        whole = max(1, self.size_a + self.size_b)
        lo = self.start
        ra = self._records(self.a, 0, counter)
        rb = self._records(self.b, 1, counter)
        batch: list[tuple[int | None, int | None]] = []
        raws_a: dict[int, Any] = {}
        raws_b: dict[int, Any] = {}
        nbytes = 0
        steps = 0
        try:
            for x, y in itertools.zip_longest(ra, rb):
                steps += 1
                if steps % CHECK_EVERY == 0:
                    check(self.cancel)
                    meter.at(lo + (1.0 - lo) * 0.98 * (counter[0] + counter[1]) / whole)
                if x is not None and y is not None:
                    if x[2] == y[2]:
                        rep.equal += 1
                        continue
                    batch.append((x[0], y[0]))
                    raws_a[x[0]] = x[1]
                    raws_b[y[0]] = y[1]
                    nbytes += len(x[1] or b"") + len(y[1] or b"")
                elif x is not None:
                    batch.append((x[0], None))
                    raws_a[x[0]] = x[1]
                    nbytes += len(x[1] or b"")
                else:
                    batch.append((None, y[0]))           # type: ignore[index]
                    raws_b[y[0]] = y[1]                  # type: ignore[index]
                    nbytes += len(y[1] or b"")           # type: ignore[index]
                if len(batch) >= BATCH_RECORDS or nbytes >= BATCH_BYTES:
                    yield from self._batch(batch, raws_a, raws_b)
                    batch, raws_a, raws_b, nbytes = [], {}, {}, 0
            if batch:
                yield from self._batch(batch, raws_a, raws_b)
        finally:
            ra.close()
            rb.close()
        rep.bytes_read += counter[0] + counter[1]

    # -- document mode -----------------------------------------------------------------------------
    def _document_mode(self) -> Iterator[RecordDiff]:
        rep = self.report
        detail = self._details_left()
        sink = _Sink(self.opts.max_changes if detail else 0, stop=not detail)
        meter = self.meter
        assert meter is not None
        meter.message(t("diff.progress.compare", a=rep.a_name, b=rep.b_name))
        nd = _NodeDiff(self.a, self.b, self.opts, sink, self.cancel,
                       progress=meter.child(self.start, 1.0, self.size_a + self.size_b))
        try:
            nd.run(self.a.root(), self.b.root())
        except _Stop:
            pass
        finally:
            nd.close()
        rep.bytes_read += self.size_a + self.size_b
        if sink.vd.equal:
            rep.equal += 1
            return
        rd = RecordDiff("changed", None, None, MISSING, sink.vd if detail else None, scope="document")
        rep.changed += 1
        if rd.diff is not None:
            rep.details += 1
            rep.moved += rd.diff.counts.get("moved", 0)
        yield rd

    # -- comparing the records found ---------------------------------------------------------------
    def _span_len(self, doc: Any, pos: int) -> int:
        s, e = doc.record_span(pos)
        return e - s

    def _emit(self, items: Iterator[tuple[int | None, int | None]]) -> Iterator[RecordDiff]:
        """Compare the ``(A position or None, B position or None)`` items in batches."""
        batch: list[tuple[int | None, int | None]] = []
        nbytes = 0
        a, b = self.a, self.b
        for pa, pb in items:
            batch.append((pa, pb))
            if pa is not None:
                nbytes += self._span_len(a, pa)
            if pb is not None:
                nbytes += self._span_len(b, pb)
            if len(batch) >= BATCH_RECORDS or nbytes >= BATCH_BYTES:
                yield from self._batch(batch, None, None)
                batch = []
                nbytes = 0
        if batch:
            yield from self._batch(batch, None, None)

    def _read_raw(self, doc: Any, positions: list[int]) -> dict[int, bytes | None]:
        """Raw bytes of the records (``None`` above the value limit: compared through the Node
        path), read in the given order."""
        out: dict[int, bytes | None] = {}
        small: list[int] = []
        limit = self.value_limit
        for p in positions:
            if self._span_len(doc, p) > limit:
                out[p] = None
            else:
                small.append(p)
        if small:
            blocks = doc.iter_blocks(small, cancel=self.cancel)
            try:
                for blk in blocks:
                    check(self.cancel)
                    if blk.huge:
                        out[int(blk.indices[0])] = None
                        continue
                    sp = _u64(blk.spans)
                    data = blk.data
                    for k, p in enumerate(blk.indices):
                        out[int(p)] = data[sp[2 * k]:sp[2 * k + 1]]
            finally:
                blocks.close()
        return out

    def _batch(self, batch: list[tuple[int | None, int | None]], raws_a: dict[int, Any] | None,
               raws_b: dict[int, Any] | None) -> Iterator[RecordDiff]:
        detail = self._details_left()
        if raws_a is None:
            need_a = [pa for pa, pb in batch if pa is not None and (pb is not None or detail)]
            raws_a = self._read_raw(self.a, need_a)
        if raws_b is None:
            need_b = [pb for pa, pb in batch if pb is not None and (pa is not None or detail)]
            raws_b = self._read_raw(self.b, need_b)
        meter = self.meter
        done = 0
        for pa, pb in batch:
            done += 1
            if done % 64 == 0:
                check(self.cancel)
            if pa is not None and pb is not None:
                rd = self._pair(pa, pb, raws_a.get(pa, MISSING), raws_b.get(pb, MISSING))
            elif pa is not None:
                rd = self._lone("removed", pa, raws_a.get(pa, MISSING))
            else:
                rd = self._lone("added", pb, raws_b.get(pb, MISSING))   # type: ignore[arg-type]
            if rd is not None:
                self._account(rd)
                yield rd
        self.out_done += len(batch)
        if meter is not None and self.mode != "position":
            meter.at(self.out_lo + (1.0 - self.out_lo) * 0.99 * min(1.0, self.out_done / self.out_total))

    def _parse(self, doc: Any, raw: bytes, wrap: bool) -> tuple[Any, Any, Any]:
        """``(value compared, record value, member key or None)`` of raw record bytes."""
        if doc.member_records:
            k, vo = split_member(raw)
            v = codec.loads(raw[vo:])
            return ({k: v} if wrap else v), v, k
        v = codec.loads(raw)
        return v, v, None

    def _display_key(self, value: Any, member_key: Any) -> Any:
        if self.mode != "key":
            return MISSING
        if self.key_path is None:
            return member_key if member_key is not None else MISSING
        return path_get(value, self.key_path) if self.key_path else value

    def _big_key(self, side: str, doc: Any, pos: int) -> Any:
        if self.mode != "key":
            return MISSING
        if self.key_path is None:
            k = doc.record_key(pos)
            return MISSING if k is None else k
        hasher = self.hashers.get(side)
        if hasher is None:  # pragma: no cover - key mode always has them
            return MISSING
        try:
            return hasher.stream_value(pos, None)
        except (CaptureLimitExceeded, *_DECODE_ERRORS):
            return MISSING
        except JsonReshapeError as exc:
            if exc.key != "stream.error.parse":
                raise
            return MISSING

    def _pair(self, pa: int, pb: int, ra: Any, rb: Any) -> RecordDiff | None:
        detail = self._details_left()
        sink = _Sink(self.opts.max_changes if detail else 0, stop=not detail)
        key: Any = MISSING
        skip = 1 if self.wrap_a else 0
        node_path = ra is None or rb is None or ra is MISSING or rb is MISSING
        if not node_path:
            try:
                va, rec_a, ka = self._parse(self.a, ra, self.wrap_a)
                vb, _rec_b, _kb = self._parse(self.b, rb, self.wrap_b)
            except _DECODE_ERRORS:
                node_path = True
            else:
                if detail:
                    key = self._display_key(rec_a, ka)
                try:
                    _Walker(self.opts, sink, skip, self.cancel).run(va, vb)
                except _Stop:
                    pass
        if node_path:
            na = self.a.record_node(pa)
            nb = self.b.record_node(pb)
            nd = _NodeDiff(self.a, self.b, self.opts, sink, self.cancel, skip=skip)
            try:
                if self.wrap_a or self.wrap_b:
                    nd.run_members(na, nb, self.wrap_a, self.wrap_b)
                else:
                    nd.run(na, nb)
            except _Stop:
                pass
            finally:
                nd.close()
            if detail:
                key = self._big_key("a", self.a, pa)
        if sink.vd.equal:
            self.report.equal += 1
            return None
        return RecordDiff("changed", pa, pb, key, sink.vd if detail else None)

    def _lone(self, status: str, pos: int, raw: Any) -> RecordDiff:
        a_side = status == "removed"
        a_index, b_index = (pos, None) if a_side else (None, pos)
        if not self._details_left():
            return RecordDiff(status, a_index, b_index)
        doc = self.a if a_side else self.b
        wrap = self.wrap_a if a_side else self.wrap_b
        key: Any = MISSING
        value: Any
        path: tuple = ()
        if raw is None or raw is MISSING:
            node = doc.record_node(pos)
            nd = _NodeDiff(doc, doc, self.opts, _Sink(0), self.cancel)
            try:
                value = nd.value_or_omitted(doc, node)
            finally:
                nd.close()
            if wrap:
                path = (node.key,)
            key = self._big_key("a" if a_side else "b", doc, pos)
        else:
            try:
                value, rec, member_key = self._parse(doc, raw, False)
            except _DECODE_ERRORS:
                node = doc.record_node(pos)
                value = Omitted(node.kind, int(node.size), node.scalar_text(OMITTED_PREVIEW))
                rec, member_key = MISSING, doc.record_key(pos)
            if wrap and member_key is not None:
                path = (member_key,)
            key = self._display_key(rec, member_key) if rec is not MISSING else MISSING
        vd = ValueDiff()
        vd.counts[status] = 1
        vd.changes.append(Change(status, path, None, value, MISSING) if a_side else
                          Change(status, None, path, MISSING, value))
        return RecordDiff(status, a_index, b_index, key, vd)


class _TooLargeWrapper(Exception):
    """A wrapper over the value limit (not compared, with a warning)."""

    def __init__(self, size: int) -> None:
        super().__init__(size)
        self.size = size


def _popcount(bits: bytearray) -> int:
    return int.from_bytes(bits, "little").bit_count() if bits else 0


# ----------------------------------------------------------------------------------
# entry points
# ----------------------------------------------------------------------------------
def iter_record_diffs(doc_a: Any, doc_b: Any, *, key: str | None = None, mode: str = "auto",
                      opts: DiffOptions | None = None, max_details: int | None = None,
                      memory_limit: int | None = None, results: bool = False, progress: Progress | None = None,
                      cancel: CancelToken | None = None, strategy: str = "auto",
                      block_bytes: int = 8 * MiB) -> Generator[RecordDiff, None, DiffReport]:
    """The differing records of two documents as they are found (module docstring "Files"): a
    generator of :class:`RecordDiff` whose return value (``StopIteration.value``, or the value
    of ``yield from``) is the :class:`DiffReport`. The arguments are checked and the documents
    pinned when this is called; closing the generator early removes every temporary file.
    ``key``: a concrete path inside the records (``$`` = the whole record) or ``$key``;
    ``mode``: ``auto`` / ``key`` / ``align`` / ``position`` / ``document``; ``memory_limit``:
    bytes for the key entries in memory (default ``Settings.memory_limit_bytes // 4``, at most
    half of that setting, like the duplicate finder);
    ``results``: keep the result table (``DiffReport.results_file``; the caller closes the
    report). Raises ``UserInputError`` (``diff.error.*``, ``dupes.error.*``),
    ``PathSyntaxError``, ``Cancelled``, ``FileChangedError``. :func:`write_diff` given this
    generator knows the compared files before it runs it (fix round R5)."""
    run = _Run(doc_a, doc_b, key=key, mode=mode, opts=opts, max_details=max_details, memory_limit=memory_limit,
               results=results, progress=progress, cancel=cancel, strategy=strategy, block_bytes=block_bytes)
    gen = run.records()
    with _RUNS_LOCK:
        _RUNS[gen] = run.report
    return gen


#: the generators :func:`iter_record_diffs` returned -> their (unfinished) reports, so that
#: :func:`write_diff` knows the compared files before it runs one (weak: gone with the generator)
_RUNS: weakref.WeakKeyDictionary[Any, DiffReport] = weakref.WeakKeyDictionary()
_RUNS_LOCK = threading.Lock()


def _run_report(diffs: Any) -> DiffReport | None:
    """The report of a generator :func:`iter_record_diffs` returned (``None`` for anything else)."""
    try:
        with _RUNS_LOCK:
            return _RUNS.get(diffs)
    except TypeError:       # not weakly referenceable (a list of record diffs): not one of ours
        return None


def _refuse_sources(path: str | os.PathLike[str], sources: Iterable[Any]) -> None:
    """``writer.error.same_file`` when ``path`` is one of the compared files (the writer's rule:
    the normalized path or, both existing, the same file under another name)."""
    for p in sources:
        if p is not None and same_file(path, p):
            raise UserInputError("writer.error.same_file", path=os.fspath(path))


def record_diff(doc_a: Any, a_index: int | None, doc_b: Any, b_index: int | None, opts: DiffOptions | None = None,
                *, key: str | None = None, cancel: CancelToken | None = None) -> RecordDiff:
    """The details of ONE record pair -- or of one record: ``b_index=None`` removed, ``a_index=None``
    added -- computed exactly as :func:`diff_files` computes them (member records as ``{key:
    value}`` unless the key is ``$key``, records over the value limit through the Node path, the
    key value shown in key mode). For the GUI's result grid, which reads ``(status, a, b)`` rows
    from ``DiffReport.results_file`` and computes the details of the visible rows: pass the run's
    ``report.key`` and ``report.options`` and the same (pinned) documents -- the indices are
    positions of the states the diff ran on (``report.a_gen`` / ``report.b_gen``). An equal pair
    gives status ``"changed"`` with an empty :class:`ValueDiff`. Raises ``IndexError`` for an
    index out of range, ``ValueError`` without any index, and like :func:`iter_record_diffs`."""
    if a_index is None and b_index is None:
        raise ValueError("record_diff needs a_index, b_index or both")
    run = _Run(doc_a, doc_b, key=key, mode="key" if key is not None else "position", opts=opts, max_details=None,
               memory_limit=None, results=False, progress=None, cancel=cancel, strategy="auto", block_bytes=8 * MiB)
    try:
        return run.one(a_index, b_index)
    finally:
        run._cleanup()


def diff_files(doc_a: Any, doc_b: Any, *, key: str | None = None, mode: str = "auto",
               opts: DiffOptions | None = None, on_record: Callable[[RecordDiff], None] | None = None,
               max_details: int | None = None, memory_limit: int | None = None, results: bool = False,
               progress: Progress | None = None, cancel: CancelToken | None = None, strategy: str = "auto",
               block_bytes: int = 8 * MiB) -> DiffReport:
    """Compare two documents (module docstring "Files"); ``on_record`` is called with every
    :class:`RecordDiff` as it is found. Arguments and errors as :func:`iter_record_diffs`."""
    gen = iter_record_diffs(doc_a, doc_b, key=key, mode=mode, opts=opts, max_details=max_details,
                            memory_limit=memory_limit, results=results, progress=progress, cancel=cancel,
                            strategy=strategy, block_bytes=block_bytes)
    try:
        while True:
            try:
                rd = next(gen)
            except StopIteration as stop:
                return stop.value
            if on_record is not None:
                on_record(rd)
    except BaseException:
        gen.close()
        raise


# ----------------------------------------------------------------------------------
# writing the differences
# ----------------------------------------------------------------------------------
def _change_obj(c: Change) -> dict[str, Any]:
    path = c.path
    obj: dict[str, Any] = {"op": c.op, "path": format_path(path), "pointer": format_path(path, "pointer")}
    if c.a_path is not None and c.b_path is not None and c.a_path != c.b_path:
        obj["a_pointer"] = format_path(c.a_path, "pointer")
        obj["b_pointer"] = format_path(c.b_path, "pointer")
    for side, v in (("a", c.a), ("b", c.b)):
        if v is MISSING:
            continue
        if isinstance(v, Omitted):
            obj[side + "_omitted"] = {"kind": v.kind, "size": v.size, "preview": v.preview}
        else:
            obj[side] = v
    return obj


def record_dict(rd: RecordDiff) -> dict[str, Any]:
    """A record diff as the JSON object :func:`write_diff` writes (``status``, ``scope`` unless
    ``"record"``, ``key``, ``a``, ``b``, ``changes`` with ``op`` / ``path`` / ``pointer`` /
    ``a`` / ``b`` (exact ``codec`` values; ``a_omitted`` / ``b_omitted`` for values too large to
    hold), ``truncated`` + ``counts``) -- e.g. for JSON Lines on stdout (``codec.dumps``)."""
    obj: dict[str, Any] = {"status": rd.status}
    if rd.scope != "record":
        obj["scope"] = rd.scope
    obj["key"] = None if rd.key is MISSING else rd.key
    obj["a"] = rd.a_index
    obj["b"] = rd.b_index
    if rd.diff is not None:
        obj["changes"] = [_change_obj(c) for c in rd.diff.changes]
        if rd.diff.truncated:
            obj["truncated"] = True
            obj["counts"] = dict(rd.diff.counts)
    return obj


def summary_dict(rep: DiffReport) -> dict[str, Any]:
    """The report's counts as the ``summary`` object :func:`write_diff` writes (warnings as texts in
    the current UI language)."""
    return {
        "mode": rep.mode, "key": rep.key, "a_records": rep.a_records, "b_records": rep.b_records,
        "equal": rep.equal, "changed": rep.changed, "added": rep.added, "removed": rep.removed, "moved": rep.moved,
        "duplicate_keys": list(rep.duplicate_keys), "missing_keys": list(rep.missing_keys),
        "skipped_keys": list(rep.skipped_keys), "wrapper_changed": rep.wrapper is not None,
        "warnings": rep.warning_texts(), "elapsed": round(rep.elapsed, 3),
    }


def _cell(v: Any) -> str:
    if v is MISSING:
        return ""
    if isinstance(v, Omitted):
        return v.text()
    try:
        text = codec.dumps(v).decode("utf-8", "replace")
    except (TypeError, ValueError):  # pragma: no cover - codec values
        text = repr(v)
    if len(text) > CSV_CELL_MAX:
        text = text[:CSV_CELL_MAX - 1] + _ELLIPSIS
    return text


class _Drain:
    """Iterates diffs and keeps the generator's return value (the report)."""

    def __init__(self, diffs: Iterable[RecordDiff]) -> None:
        self.it = iter(diffs)
        self.report: DiffReport | None = None

    def __iter__(self) -> Iterator[RecordDiff]:
        while True:
            try:
                rd = next(self.it)
            except StopIteration as stop:
                if isinstance(stop.value, DiffReport):
                    self.report = stop.value
                return
            yield rd

    def close(self) -> None:
        close = getattr(self.it, "close", None)
        if close is not None:
            close()


def write_diff(diffs: Iterable[RecordDiff], path: str | os.PathLike[str], fmt: str = "json", *,
               report: DiffReport | None = None, sources: Iterable[str | os.PathLike[str]] | None = None) -> int:
    """Write record diffs to ``path`` (atomically) and return how many were written.

    ``fmt``: ``"json"`` (``{"records": [...], "summary": {...}}``, streamed), ``"jsonl"`` (one
    object per record, then ``{"summary": ...}``) or ``"csv"`` (one row per change: ``status, key,
    a_index, b_index, op, path, a_value, b_value``; values as compact JSON, an added / removed record
    without details as one row). A record object: ``status``, ``key``, ``a``, ``b`` (0-based record
    numbers), ``changes`` (``op``, ``path`` dotted, ``pointer``, ``a`` / ``b`` values -- exact
    numbers --, ``a_pointer`` / ``b_pointer`` where the sides differ, ``a_omitted`` / ``b_omitted``
    for values too large to hold). The summary comes from ``report`` or from the return value of
    a :func:`iter_record_diffs` generator; without either it is left out.

    ``path`` must not be a compared file (``UserInputError("writer.error.same_file")``, nothing
    written, ``diffs`` closed): the sources and data copies of ``report.sources``, of the generator
    :func:`iter_record_diffs` returned (``diffs`` itself) and ``sources`` are refused before
    anything runs; those of another generator's returned report before the result replaces
    anything (fix round R5, R5F-2). A caller with neither a report nor such a generator passes the
    documents' files as ``sources``."""
    kind = fmt.strip().lower().lstrip(".") if isinstance(fmt, str) else None
    if kind not in ("json", "jsonl", "csv"):
        raise UserInputError("diff.error.format", value=repr(fmt))
    known = [r for r in (report, _run_report(diffs)) if r is not None]
    named = [sources] if isinstance(sources, (str, bytes, os.PathLike)) else list(sources or ())
    drain = _Drain(diffs)
    count = 0
    try:
        _refuse_sources(path, named + [p for r in known for p in r.sources])
        with atomic_write(path, "wb") as f:
            if kind == "json":
                f.write(b'{"records":[')
                for rd in drain:
                    f.write((b"\n" if count == 0 else b",\n") + codec.dumps(record_dict(rd)))
                    count += 1
                f.write(b"\n]")
                rep = report or drain.report
                if rep is not None:
                    f.write(b',\n"summary":' + codec.dumps(summary_dict(rep)))
                f.write(b"}\n")
            elif kind == "jsonl":
                for rd in drain:
                    f.write(codec.dumps(record_dict(rd)) + b"\n")
                    count += 1
                rep = report or drain.report
                if rep is not None:
                    f.write(codec.dumps({"summary": summary_dict(rep)}) + b"\n")
            else:
                settings = get_settings()
                w = io.TextIOWrapper(f, encoding="utf-8-sig" if settings.csv_bom else "utf-8", newline="")
                try:
                    out = csv.writer(w, delimiter=settings.csv_delimiter, lineterminator="\r\n")
                    out.writerow(["status", "key", "a_index", "b_index", "op", "path", "a_value", "b_value"])
                    for rd in drain:
                        key = _cell(rd.key)
                        a_i = "" if rd.a_index is None else rd.a_index
                        b_i = "" if rd.b_index is None else rd.b_index
                        status = rd.status if rd.scope == "record" else f"{rd.status}:{rd.scope}"
                        changes = rd.diff.changes if rd.diff is not None else []
                        if not changes:
                            out.writerow([status, key, a_i, b_i, rd.status, "", "", ""])
                        for c in changes:
                            out.writerow([status, key, a_i, b_i, c.op, format_path(c.path), _cell(c.a), _cell(c.b)])
                        count += 1
                    w.flush()
                finally:
                    w.detach()
            if drain.report is not None:    # a generator's report, known only now: before the commit
                _refuse_sources(path, drain.report.sources)
    except BaseException:
        drain.close()
        raise
    return count
