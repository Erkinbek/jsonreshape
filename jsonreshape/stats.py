"""Per-field statistics (docs/ARCHITECTURE.md §14.1; design: scratchpad ``s5/map_stats.md`` §4;
AGENT_PROMPT §9 "Maydon statistikasi").

:func:`field_stats` ``(doc, fields, source=None, *, top=20, exact=False, sample=None,
memory_limit=None, progress=None, cancel=None, on_partial=None)`` computes, in ONE pass over the
records of a scope, for every requested field (any :mod:`jsonreshape.paths` pattern, or ``$key``):
presence (present / ``null`` / missing), the type distribution, the number of distinct values, the
most frequent values with their counts, exact number statistics (min / max / sum / mean, integers),
string lengths, array lengths and object key counts. The result is one :class:`FieldStats` per
requested field (same order).

Semantics (the schema's, so the schema view and the statistics agree)
----------------------------------------------------------------------
* **Scope**: ``source`` = ``None`` (every record; the index is built first when needed), a
  RecordSet (``gen`` must be ``None`` or the pinned state's), a ``range``, a list / array or any
  iterable of positions. A position named twice is examined once (like :mod:`jsonreshape.dupes`).
  ``sample=N``: the evenly spread sample of :func:`jsonreshape.schema.sample_positions` of the
  scope. *Scope order* is the order of ``source`` (the current view's order in the GUI).
* **Values**: the values a pattern matches in a record with dict semantics (the last of
  duplicate keys wins, at the place of the first), as ``core.PathExtractor`` / ``PathPattern.values``
  give them; member records (keyed documents, wrapped documents with an object container): the
  member *value*; ``$key``: the member key (no values in other documents: warning
  ``stats.warn.no_record_keys``).
* **Presence** (``SchemaBuilder``'s rule, module docstring of :mod:`jsonreshape.schema`): the
  *context* of a field is its nearest array-element ancestor (the last ``[*]`` before the last
  segment; the field itself excluded), or the record. ``base`` = contexts examined (records, or
  the elements of the context pattern ``C``), ``present`` = contexts holding at least one value,
  ``missing = base - present``; ``null`` is present. Concrete keys and indices select at most one
  value per parent; with only such segments after ``C``, ``present`` is the number of values;
  otherwise (a ``*`` after ``C``, or a field ending in ``[*]``) a context counts when one of its
  values lies inside it (spans in the fast path, structure in the others). Patterns with ``..`` /
  ``**`` have the record as their context (documented: their matches have no common context).
* **Equality** is the canonical equality of :mod:`jsonreshape.dupes` (numbers by exact decimal
  value -- ``1 == 1.0 == 1e0``, ``-0 == 0``, ``1.10 == 1.1``; strings exactly, escapes decoded;
  containers with sorted members; ``1 != true != "1"``). ``count`` = occurrences of a value,
  ``records`` = records holding it (``== count`` when a record holds at most one value).
* **Order of the top values**: count descending, then the first occurrence (the first record in
  scope order, then document order inside that record). ``first`` = the position of that record
  ("Daraxtda ko'rsatish"). A value written in several spellings is ONE value; the spelling shown
  (``text``, ``value``, number min/max texts) is the spelling of its first occurrence -- one rule
  for every mode (documented deviation from map_stats §4.2 "the most frequent spelling": the
  spilled exact mode and the sketches keep no per-spelling counts, and the first occurrence is the
  record "show in tree" opens).
* **Numbers** (§12 "Raqamlar aniqligi"): integers are Python ints (no digit limit for the sum;
  ``int()`` is used only below 4 000 digits), other tokens exact ``Decimal`` values; min / max are
  compared exactly and keep the token text of their first occurrence (``1e400``,
  ``12345678901234567890``); ``sum_text`` is the exact sum with the finest scale of the numbers'
  spellings (``1.10 + 2.20 = 3.30``; the same in every mode -- dicts that key a value by its
  canonical token note the scale of each spelling they fold in; an addition that would need more than
  :data:`SUM_DIGITS_MAX` digits -- ``1e999999 + 1`` -- switches to 50 significant digits,
  ``exact_sum=False`` and warning ``stats.warn.sum_approximate``); ``mean_text`` = sum / count in 34
  digits, shown with 15 significant digits (trailing zeros removed). Numbers whose exponent is
  beyond about ``10**15`` (``1e99999999999999999999``) are left out of the sum and the mean (the
  mean is over the others; ``"—"`` when none is left): ``NumberStats.sum_skipped``,
  ``exact_sum=False`` and warning ``stats.warn.sum_skipped`` (review R5A-11). ``integers`` counts
  integral values (``1.0`` and ``1e400`` included, like the schema's counter).
* **Strings**: lengths in code points (like ``len(str)``), ``empty`` = ``""`` occurrences.

Algorithm (map_stats §4.3)
--------------------------
* One ``core.PathExtractor`` for every requested pattern and the context patterns ``C``, run over
  :data:`CHUNK_RECORDS` records at a time (cancellation is checked per chunk); ``..`` / ``**``
  patterns (and every pattern with the pure-Python core, ``strategy="auto"``) use the parse
  strategy (``codec.loads`` + ``PathPattern.values``; records over :data:`PARSE_MAX_BYTES` are
  streamed instead). Both strategies give identical results.
* **Exact phase**: per field a dict from the token to one packed int (count, records, first
  occurrence, last record). Fields that hold at most one value per record use the raw token as the
  key (spellings are merged at the end); others use the canonical token of the value (a spelling
  check makes this cheap for the common canonical spellings), so ``records`` counts every record
  once whatever spellings it holds. Tokens longer than :data:`LONG_TOKEN` bytes (and containers
  of multi-valued fields) are keyed by ``b"#" + blake2b-128`` of their canonical form; their
  type / length / number statistics are counted per occurrence, everything else is derived from
  the distinct values at the end.
* **Budget**: ``memory_limit`` or ``Settings.memory_limit_bytes // 8``, shared by the fields,
  accounted per entry as ``len(key) + 100`` bytes (+20 for multi-valued fields). It is checked
  after every chunk, every :data:`FEED_SLICE` values inside a chunk (one record can hold millions
  of values: review R5A-1) and after every record of the parse strategy. Over it, the field with
  the most entries leaves the exact phase -- in the middle of a record too:
  - default: **sketches** -- HyperLogLog (p = 14, 16 384 one-byte registers, the low 64 bits of
    the blake2b-128 hash; Ertl's improved estimator, unbiased over the whole range -- the classic
    one with linear counting below ``2.5 m`` was biased by up to +2.9 % above the switch, review
    R5A-6; relative standard error 1.04 / 128 = 0.81 %, :data:`HLL_RSE`) for ``distinct`` and
    Misra-Gries with ``k`` counters and batch decrement for the top values (each kept count ``c``
    of a value satisfies ``c <= true count <= c + D``, ``D = FieldStats.top_bound <= values / (k +
    1)``; ``records`` likewise). ``k`` = :data:`MG_COUNTERS`, fewer when the sketches of the call
    would hold more than half the budget (``k`` shared by the sketched fields, at least
    ``max(MG_MIN, 2 * top)``; a summary holds up to ``2k`` counters, :data:`MG_ENTRY_COST` bytes
    each: review R5A-2). Both are seeded from the exact counts; types, numbers and lengths stay
    exact (counted per occurrence). Hashing is blake2b: deterministic across processes (never
    Python's ``hash()``).
  - the analyses of short tokens are cached once for all fields (at most :data:`CACHE_MAX` of
    them and an eighth of the budget); so the dicts use at most the budget, the sketches at most
    half of it (unless ``MG_MIN`` counters per field need more), the caches an eighth, plus what
    one :data:`FEED_SLICE` adds; the quads of a chunk are a view of the extractor's output, 32
    bytes per value (a record with millions of values still costs that much while it is read).
  - ``exact=True`` ("Aniq hisoblash", ``--exact``): the dict is spilled as one sorted run of
    aggregated entries ``hash96 | first record (40 bits) | first inside the record (15) | flag (1)
    | count (20) | records (20)`` (one entry per distinct value of the epoch, split when a count
    needs more bits) through :class:`jsonreshape.dupes.HashRuns` (``sort-<pid>-<hex>`` in the work
    folder, removed at the end, also on errors and cancellation), and a new dict starts. The final
    merge walks equal hashes: distinct = groups, exact counts and records (the epochs of the call
    hold disjoint records -- except a record a multi-valued field spilled in the middle of: the
    entries of both epochs leave that record out of their ``records`` and a flagged *marker*
    entry (count 0) stands for it; the epochs of ONE streamed record are flagged and count that
    record once per value), the first occurrence = the smallest entry. Low-cardinality fields
    never spill.
    (Deviation from map_stats §4.3: entries are aggregated per epoch instead of one per
    occurrence, so :func:`jsonreshape.dupes.iter_groups` -- which counts entries -- is not used;
    the exact dict phase runs first, as the design allows.)
* **Huge records** (``Block.huge``) and records the fast paths cannot read: one
  :func:`jsonreshape.stream.stream_probe` pass feeds per-record accumulators (scalars by their
  compact JSON text, containers by type and length); a field whose pattern matched containers
  gets their canonical values from a second pass (:func:`~jsonreshape.stream.stream_values`
  within :func:`~jsonreshape.stream.capture_budget`; beyond it the containers count in types and
  lengths but not in distinct / top: ``FieldStats.uncounted`` + warning). The per-record
  accumulators are merged when the record succeeded -- a probe cannot take back what it has seen
  when :class:`~jsonreshape.stream.DuplicateKeys` stops the pass; the record is then read field by
  field with ``stream_values`` (dict semantics) and a field whose values exceed the budget skips
  the record (``FieldStats.skipped`` + warning ``stats.warn.too_large``). The ijson deviations of
  :mod:`jsonreshape.stream` apply to streamed records only (``-0`` reads as ``0``, exponents are
  respelled ``1E+400``: same values; a lone surrogate escape reads as ``"?"``).
* **Finish**: spellings merged, exact numbers from the distinct values, the top values by
  ``heapq``; display texts of top values without a kept token (long values, spilled / sketched
  entries) are read again from their first records (at most ``top`` records per field; number
  extremes keep their token texts; each field's tokens in such a record are read and hashed
  once, ``cancel`` is checked: review R5A-5). ``on_partial(list[FieldStats])`` receives, from the
  calling thread, about every second (at least five times the cost of the previous snapshot
  apart) snapshots with ``partial=True``: presence, types, the top values by their raw spellings
  and ``distinct`` so far (values, not spellings: ``1`` and ``1.0`` are one; ``distinct_exact=False``);
  no number or string statistics.
  A field of the exact mode that has spilled shows the HyperLogLog estimate of its spilled epochs
  (at least the current epoch's count) and the top values of a small Misra-Gries summary of them
  (:data:`SNAP_MG` or ``10 * top`` counters) plus the current epoch -- they grow with the pass
  instead of dropping to the current epoch after every spill (review R5A-7); ``top_bound`` is
  that summary's ``D``.

Histograms (stage 7; docs/ARCHITECTURE.md §14.1 "as built")
-----------------------------------------------------------
* Every numeric field keeps, besides its exact extremes, the material of a **histogram**: the exact
  ``value -> count`` of its distinct finite numbers while they are at most ``k`` (:data:`HIST_SAMPLE`
  = 65 536; fewer under a small budget: ``k = max(HIST_MIN, min(HIST_SAMPLE, (budget / HIST_SHARE) /
  (fields x HIST_ENTRY_COST)))``), else a uniform **reservoir sample** of ``k`` occurrences (Li's
  Algorithm L with weighted insertions -- a dict entry is folded as ``count`` occurrences -- and merges
  that draw the union's sample and re-seat the threshold from ``Beta(k, n - k + 1)``; the generator is
  seeded with blake2b of the field text, so a run is reproducible). Numbers beyond the Decimal range
  (``1e99999999999999999999``) are left out (``_Sample.skipped``). In the exact phase the numbers reach
  the sample when the dict is folded (at the end of the pass, or at a spill), so the sample is exact
  whenever the distinct numbers of the field fit ``k``; sketched fields feed it per occurrence.
  :meth:`FieldStats.histogram` ``(bins=32)`` gives :class:`Histogram` ``(edges, counts, exact,
  sample_size)``: "nice" edges (:func:`nice_edges`: steps 1, 2, 2.5, 5 x 10^k covering min..max with
  at most ``bins`` bins; an integral field gets integer edges and half-open bins whose last edge lies
  beyond the maximum, so every integer has its own bin while they fit; a single distinct value is one
  bin ``[v, v]``), exact counts, or the sample's counts scaled to the number of values (``exact=False``,
  ``sample_size`` = the sample). Edges are exact ``int`` / ``Decimal`` values (:func:`number_text` for
  their JSON text: ``1E+400`` stays exact, never a float).
* **String lengths**: exact counts per length up to :data:`LEN_COUNTS` (4 096) distinct lengths, then
  grouped by a doubling width (``exact=False``, ``_LenCounts.width``); :meth:`FieldStats.length_histogram`.
* Memory: a sample holds at most ``k`` numbers (:data:`HIST_ENTRY_COST` = 200 accounted bytes each; all
  samples of a call at most a quarter of the budget, with the floor :data:`HIST_MIN` = 1 024 like
  ``MG_MIN``), the length counts at most 4 096 entries; both are in ``_AccSet.total()`` but not in the
  sketch bytes that resize Misra-Gries, so every earlier result is unchanged. Live snapshots carry the
  samples too (the current dict is folded into a copy). ``to_dict()`` and the CLI output are unchanged;
  :func:`format_histogram` renders a histogram as text (``jsonreshape stats --histogram``).

Decisions and extensions (documented deviations of map_stats §4)
----------------------------------------------------------------
* The spelling shown is the first occurrence's (above), not the most frequent one.
* ``exact=True`` spills aggregated epochs, not one entry per occurrence (above).
* Huge records go through per-record accumulators merged on success (above); a single huge
  record is one unit for ``on_partial`` (no snapshot inside it).
* ``..`` / ``**`` fields have the record as their presence context; concrete indices
  (``orders[0].sum``) select like keys.
* ``ValueCount.value`` is ``MISSING`` for values longer than :data:`VALUE_DECODE_BYTES` (the
  text is a cut raw prefix) and for values that could not be read again; in sketch mode ``first``
  may be a later record holding the value (a counter re-created after a batch decrement).
* Numbers beyond the Decimal exponent range (``1e99999999999999999999``) compare as +-infinity
  (tiny ones as 0) and are left out of the sum and the mean (``exact_sum=False``,
  ``NumberStats.sum_skipped``, warning ``stats.warn.sum_skipped``).
* A record that may hold more values than ``Settings.memory_limit_bytes / 256`` (its commas; 2 M
  by default) is streamed for multi-valued extract fields instead of extracted (bounded memory;
  the ijson deviations of streamed records then apply to it, review R5A-1).
* Extra keyword ``strategy`` (``"auto"`` / ``"extract"`` / ``"parse"``, like ``find_duplicates``),
  extra :class:`FieldStats` fields (``uncounted``, ``multi``, ``context``, ``partial``),
  :meth:`FieldStats.to_dict` / :meth:`~FieldStats.warning_texts` / :meth:`~FieldStats.format_text`
  and :func:`format_stats` (the localized CLI text).

Documents, progress, errors
---------------------------
``doc.pinned()`` first (one overlay state for the whole call; positions are its positions); a
``source`` of another state raises ``EditError("edits.error.stale_scope")``. Progress is reported
in bytes (index build when needed, then the records read, then the final merge of spilled runs);
``Cancelled`` when ``cancel`` fires (temporary files removed). ``doc.ensure_unchanged()`` runs
before the results are returned (``FileChangedError``). Bad arguments raise ``UserInputError``
(``stats.error.*``) and ``PathSyntaxError``.
"""

from __future__ import annotations

import bisect
import hashlib
import heapq
import itertools
import json
import logging
import math
import random
import re
import time
from array import array
from dataclasses import dataclass, field as dc_field
from decimal import MAX_EMAX, MAX_PREC, MIN_EMIN, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Context, Decimal
from typing import Any, Callable, Iterable, NamedTuple, Sequence

from . import codec, core
from .appdirs import work_dir
from .dupes import HashRuns, canonical_bytes, canonical_token, key_hash
from .errors import Cancelled, EditError, JsonReshapeError, UserInputError
from .i18n import space_grouped, t
from .paths import MISSING, PathPattern, Wild, parse_pattern
from .query import render_path
from .reader import split_member as _split_member
from .recordset import RecordSet, _warning_texts
from .schema import _escape_display, example_text, sample_positions
from .settings import get_settings
from .stream import CaptureLimitExceeded, DuplicateKeys, capture_budget, stream_probe, stream_values
from .tasks import CancelToken, Progress, check, format_bytes

log = logging.getLogger(__name__)

__all__ = [
    "CHUNK_RECORDS",
    "DEFAULT_TOP",
    "HIST_MIN",
    "HIST_SAMPLE",
    "HLL_P",
    "HLL_RSE",
    "KEY_FIELD",
    "LEN_COUNTS",
    "LONG_TOKEN",
    "MG_COUNTERS",
    "SUM_DIGITS_MAX",
    "TOP_MAX",
    "FieldStats",
    "Histogram",
    "NumberStats",
    "StringStats",
    "ValueCount",
    "bin_label",
    "field_stats",
    "format_histogram",
    "format_stats",
    "nice_edges",
    "number_text",
]

KiB = 1 << 10
MiB = 1 << 20

KEY_FIELD = "$key"           # the member key pseudo-field
DEFAULT_TOP = 20
TOP_MAX = 10000              # top values asked for at most
CHUNK_RECORDS = 4096         # records per extraction (cancellation and budget checks)
LONG_TOKEN = 64              # longer tokens are keyed by their canonical hash
ENTRY_COST = 100             # accounted bytes of a dict entry besides its key
MULTI_EXTRA = 20             # ... more for a multi-valued field (wider packed int)
HLL_P = 14
HLL_M = 1 << HLL_P
HLL_RSE = 1.04 / math.sqrt(HLL_M)   # 0.008125
MG_COUNTERS = 10000
MG_MIN = 1000                # Misra-Gries counters per sketched field at least (module docstring, "Budget")
MG_ENTRY_COST = 250          # accounted bytes of a Misra-Gries counter
CACHE_ENTRY_COST = 200       # accounted bytes of a cached token analysis (besides twice the token's length)
FEED_SLICE = 16384           # values fed between two budget checks inside a chunk
EXTRACT_VALUE_SHARE = 256    # a record with more commas than memory_limit_bytes / this is streamed (multi-valued)
SNAP_MG = 200                # counters of the live-snapshot summary of a spilled field (at least; 10 x top)
SUM_DIGITS_MAX = 10000       # an exact addition needing more digits switches to 50 significant digits
MEAN_DIGITS = 15             # significant digits of mean_text
TEXT_CHARS = 120             # display texts of values are cut to this many characters
VALUE_DECODE_BYTES = 1 * MiB  # ValueCount.value is MISSING for longer tokens
PARSE_MAX_BYTES = 1 * MiB    # the parse strategy streams bigger records instead of parsing them
PARTIAL_SECONDS = 1.0        # on_partial interval (at least)
PARTIAL_COST_FACTOR = 5.0    # ... and at least this many times the cost of the last snapshot
CACHE_MAX = 65536            # tokens remembered by the normalization / sketch caches
CACHE_TOKEN_MAX = 256        # longer tokens are not cached
TICK_VALUES = 4096           # streamed values between budget checks
FINISH_SHARE = 0.05          # share of the progress bar for the final merge (exact mode)
INDEX_SHARE = 0.45           # share of the progress bar for an index build done here
HIST_SAMPLE = 65536          # numbers kept per numeric field for the histogram (exact values, else a reservoir)
HIST_MIN = 1024              # ... at least, whatever the budget (like MG_MIN)
HIST_ENTRY_COST = 200        # accounted bytes of one kept number (the value and its dict entry / list slot)
HIST_SHARE = 4               # the histogram samples of a call may use the budget / this
HIST_BINS = 32               # bins of FieldStats.histogram by default
HIST_BINS_MAX = 4096         # bins asked for at most
HIST_INT_MAX_DIGITS = 6000   # integer histogram edges are built only up to this many digits; an integral range
                             # larger than that (e.g. a field holding 1e999999) uses real-number edges instead,
                             # because exact integer edges would need that many digits of Decimal precision (minutes)
                             # and consecutive integers are indistinguishable at that scale
LEN_COUNTS = 4096            # distinct string lengths counted exactly; beyond: grouped by a doubling width
ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"

T_OBJECT, T_ARRAY, T_STRING, T_NUMBER, T_BOOL, T_NULL = range(6)
TYPE_NAMES = ("object", "array", "string", "number", "bool", "null")

_M15 = (1 << 15) - 1
_M20 = (1 << 20) - 1
_M24 = (1 << 24) - 1
_M40 = (1 << 40) - 1
_M64 = (1 << 64) - 1
_REC_INC = (1 << 40) + 1      # multi-valued packed entry: records + 1, count + 1
_DICT, _SKETCH, _SPILL = 0, 1, 2

_EXACT = Context(prec=MAX_PREC, Emax=MAX_EMAX, Emin=MIN_EMIN)
_APPROX = Context(prec=50, Emax=MAX_EMAX, Emin=MIN_EMIN)
_MEAN = Context(prec=34, Emax=MAX_EMAX, Emin=MIN_EMIN)
_ROUND = Context(prec=MEAN_DIGITS, rounding=ROUND_HALF_EVEN, Emax=MAX_EMAX, Emin=MIN_EMIN)
_H_EMPTY = hashlib.blake2b(digest_size=16)
_H_STRING = hashlib.blake2b(b"s", digest_size=16)
_scanstring = json.decoder.scanstring
_from_bytes = int.from_bytes
_LITERALS = {0x74: (b"true", T_BOOL, b"t"), 0x66: (b"false", T_BOOL, b"f"), 0x6E: (b"null", T_NULL, b"z")}
_DECODE_ERRORS = (codec.JsonDecodeError, ValueError, RecursionError, UnicodeDecodeError, ArithmeticError)

# test hook: the PathExtractor class (None = core.PathExtractor)
_EXTRACTOR: Any = None


# ----------------------------------------------------------------------------------
# results
# ----------------------------------------------------------------------------------
@dataclass
class ValueCount:
    """One of the most frequent values of a field.

    ``value``: the value as :func:`jsonreshape.codec.loads` gives it (exact numbers:
    ``JsonFloat`` / ``JsonInt``; containers parsed), the spelling of its first occurrence;
    :data:`~jsonreshape.paths.MISSING` when its JSON text is longer than
    :data:`VALUE_DECODE_BYTES` or could not be read again. ``text``: a safe display text (strings
    quoted and escaped like ``schema.example_text``, control / bidi characters as ``\\uXXXX``, cut
    to :data:`TEXT_CHARS` characters with ``…``). ``count``: occurrences; ``records``: records
    holding it; ``first``: the position of the first record (scope order) holding it; ``error``:
    approximate mode -- the true count lies in ``[count, count + error]`` (``records`` too)."""

    value: Any
    text: str
    count: int
    records: int
    first: int
    error: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible: ``json`` is the exact compact JSON text of the value (``None`` when
        the value is not available)."""
        js: str | None
        if self.value is MISSING:
            js = None
        else:
            try:
                js = codec.dumps(self.value).decode("utf-8")
            except (TypeError, ValueError):  # pragma: no cover - values come from codec
                js = None
        return {"text": self.text, "json": js, "count": self.count, "records": self.records, "first": self.first,
                "error": self.error}


@dataclass
class NumberStats:
    """Exact number statistics of a field (module docstring): ``count`` numbers, ``integers``
    integral ones; ``min_text`` / ``max_text`` the token texts of the first occurrences of the
    extremes at positions ``min_first`` / ``max_first``; ``sum_text`` (exact unless
    ``exact_sum`` is False), ``mean_text`` (15 significant digits). ``sum_skipped``: numbers left
    out of the sum and the mean (exponents beyond about ``10**15``; warning
    ``stats.warn.sum_skipped``); the mean is over the others (``"—"`` when none is left)."""

    count: int
    integers: int
    min_text: str
    max_text: str
    min_first: int
    max_first: int
    sum_text: str
    mean_text: str
    exact_sum: bool = True
    sum_skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"count": self.count, "integers": self.integers, "min": self.min_text, "max": self.max_text,
                "min_first": self.min_first, "max_first": self.max_first, "sum": self.sum_text,
                "mean": self.mean_text, "exact_sum": self.exact_sum, "sum_skipped": self.sum_skipped}


@dataclass
class StringStats:
    """String lengths in code points (like ``FieldInfo.str_len``) and the number of ``""``."""

    min_len: int
    max_len: int
    empty: int

    def to_dict(self) -> dict[str, Any]:
        return {"min_len": self.min_len, "max_len": self.max_len, "empty": self.empty}


class Histogram(NamedTuple):
    """A histogram of :meth:`FieldStats.histogram` / :meth:`FieldStats.length_histogram` (module
    docstring, "Histograms"): ``edges`` -- ``bins + 1`` ascending exact values (``int`` for integral
    data, else ``Decimal``); bin ``i`` is ``[edges[i], edges[i + 1])``, the last bin of non-integral data
    is closed (integral data: the last edge lies beyond the maximum); one bin ``[v, v]`` for a single
    distinct value. ``counts``: occurrences per bin -- exact, or the sample's counts scaled to the
    number of values when ``exact`` is False; ``sample_size``: the values the counts rest on (all of
    them when exact)."""

    edges: list
    counts: list
    exact: bool
    sample_size: int

    @property
    def bins(self) -> int:
        return len(self.counts)

    @property
    def total(self) -> int:
        return sum(self.counts)


def number_text(value: Any) -> str:
    """The JSON number text of an exact edge value: an ``int`` plainly (beyond Python's digit limit
    through Decimal), a ``Decimal`` without trailing zeros, in scientific form beyond 40 digits
    (``1E+400``) -- valid in JSON and in the filter language, never a float."""
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return str(value)
        except ValueError:            # more than 4 300 digits
            value = Decimal(value)
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    if not d.is_finite():
        return str(d)
    if d.is_zero():
        return "0"
    return _plain(d.normalize(_EXACT))


_NICE = (Decimal(1), Decimal(2), Decimal("2.5"), Decimal(5))


def _dec_digits(value: Any) -> int:
    if isinstance(value, Decimal):
        return len(value.as_tuple().digits)
    return _digits(int(value))


def _edge_digits(x: Any) -> int:
    """Decimal digits of the integer part of ``x`` -- from a ``Decimal``'s exponent, so a huge value
    (``1E+999999``) is never expanded to a full-length ``int`` just to measure it."""
    if isinstance(x, Decimal):
        return x.adjusted() + 1 if x and x.is_finite() else 1
    return _digits(int(x))


def nice_edges(lo: Any, hi: Any, bins: int = HIST_BINS, integral: bool = False) -> list:
    """The "nice" bin edges covering ``lo..hi`` (exact ``int`` / ``Decimal`` values; module docstring,
    "Histograms"): the smallest step ``1, 2, 2.5, 5 x 10^k`` that gives at most ``bins`` bins when the
    edges are aligned to multiples of the step (the first edge at or below ``lo``). ``integral``: the
    step is an integer (at least 1), the edges are ``int`` and the last edge is the first multiple of
    the step beyond ``hi`` (half-open bins for every integer). ``lo == hi``: ``[lo, hi]`` (one bin); one bin
    asked for over a range with 0 inside it: the exact extremes (no aligned step gives one bin there).
    An ``integral`` range wider than :data:`HIST_INT_MAX_DIGITS` digits (e.g. a field holding ``1e999999``) is
    treated as non-integral instead: exact integer edges would be numbers with that many digits and need that much
    Decimal precision, and integers are indistinguishable at that scale.
    ``ValueError`` for ``bins`` outside ``1..HIST_BINS_MAX``."""
    if isinstance(bins, bool) or not isinstance(bins, int) or bins < 1 or bins > HIST_BINS_MAX:
        raise ValueError(f"bins must be 1..{HIST_BINS_MAX}, got {bins!r}")
    if isinstance(lo, Decimal) and not lo.is_finite() or isinstance(hi, Decimal) and not hi.is_finite():
        raise ValueError("histogram edges need finite extremes")
    if integral and (_edge_digits(lo) > HIST_INT_MAX_DIGITS or _edge_digits(hi) > HIST_INT_MAX_DIGITS):
        # astronomically large (e.g. a field holding 1e999999): exact integer edges would be numbers with that
        # many digits and need that much Decimal precision (minutes); consecutive integers are indistinguishable
        # at that scale, so build compact real-number edges (scientific values) instead
        integral = False
    if integral:
        lo, hi = int(lo), int(hi)
    else:
        lo = lo if isinstance(lo, Decimal) else Decimal(int(lo))
        hi = hi if isinstance(hi, Decimal) else Decimal(int(hi))
    if hi < lo:
        lo, hi = hi, lo
    if lo == hi:
        return [lo, hi]
    if bins == 1 and lo < 0 and (hi > 0 or (integral and hi >= 0)):
        # one bin over a range with 0 inside it: edges aligned to a step always need two bins (one on
        # each side of 0), so the one bin is the exact extremes
        return [lo, hi + 1] if integral else [lo, hi]
    prec = max(_dec_digits(lo), _dec_digits(hi), 1) + 60
    ctx = Context(prec=prec, Emax=MAX_EMAX, Emin=MIN_EMIN)
    dlo, dhi = Decimal(lo), Decimal(hi)
    raw = ctx.divide(ctx.subtract(dhi, dlo), Decimal(bins))
    k = raw.adjusted()
    if integral and k < 0:
        k = 0
    while True:
        for m in _NICE:
            step = m.scaleb(k, context=ctx)
            if step < raw:
                continue
            if integral and step != step.to_integral_value():
                continue
            first = ctx.multiply(ctx.divide(dlo, step).to_integral_value(rounding=ROUND_FLOOR), step)
            q = ctx.divide(dhi, step)
            if integral:
                last = ctx.multiply(ctx.add(q.to_integral_value(rounding=ROUND_FLOOR), Decimal(1)), step)
            else:
                last = ctx.multiply(q.to_integral_value(rounding=ROUND_CEILING), step)
            n = int(ctx.divide(ctx.subtract(last, first), step))
            if n <= bins:
                edges = [ctx.add(first, ctx.multiply(Decimal(i), step)) for i in range(n + 1)]
                return [int(e) for e in edges] if integral else edges
        k += 1


def bin_label(edges: Sequence[Any], i: int) -> str:
    """The localized text of bin ``i`` of ``edges``: ``lo – hi`` of integral bins (inclusive; the
    integer itself for a one-integer bin), ``lo ≤ x < hi`` else (``≤ hi`` for the last bin), ``v`` for
    the single bin ``[v, v]``."""
    lo, hi = edges[i], edges[i + 1]
    if lo == hi:
        return t("stats.hist.single", value=number_text(lo))
    if isinstance(lo, int) and isinstance(hi, int):
        if hi - 1 == lo:
            return t("stats.hist.single", value=number_text(lo))
        return t("stats.hist.range_int", lo=number_text(lo), hi=number_text(hi - 1))
    key = "stats.hist.range_last" if i == len(edges) - 2 else "stats.hist.range"
    return t(key, lo=number_text(lo), hi=number_text(hi))


def format_histogram(hist: Histogram, *, width: int = 30) -> str:
    """A localized plain-text block of a histogram: a title (bins, values; the sample size when the
    counts are scaled from a sample) and one line per bin with its label, count, share and a bar of
    at most ``width`` characters (``jsonreshape stats --histogram``; the GUI draws the same data)."""
    nbins = len(hist.counts)
    total = hist.total
    key = "stats.hist.title" if hist.exact else "stats.hist.title_sampled"
    lines = [_tn(key, nbins, bins=nbins, count=_count_text(total), sample=_count_text(hist.sample_size))]
    labels = [bin_label(hist.edges, i) for i in range(nbins)]
    lw = min(max((len(x) for x in labels), default=1), 40)
    peak = max(hist.counts) if hist.counts else 0
    for label, c in zip(labels, hist.counts):
        bar = "#" * (round(c / peak * width) if peak else 0)
        lines.append(f"    {label:<{lw}}  {_count_text(c):>12}  {_share(c, total):>7}  {bar}")
    return "\n".join(lines)


@dataclass
class FieldStats:
    """Statistics of one field (module docstring for every rule).

    ``field``: the pattern in expression syntax (``query.render_path``; ``$key``); ``path``: its
    segments (``Wild.ANY_INDEX`` / ``ANY_KEY`` / ``DESCENDANTS``; ``("$key",)`` for the key);
    ``scanned`` / ``skipped``: records examined / not readable for this field (huge values over
    the capture budget, undecodable bytes); ``base`` / ``present`` / ``missing``: presence in the
    contexts (``context``: ``"$"`` = the record, else the ``[*]`` pattern); ``values``: occurrences,
    ``nulls`` of them ``null``; ``types``: occurrences per JSON type, most frequent first;
    ``distinct`` (exact, or the HyperLogLog estimate with relative standard error
    ``distinct_rse``); ``top`` (:class:`ValueCount`; ``top_exact`` False: Misra-Gries counts that
    may be up to ``top_bound`` too low); ``number`` / ``string`` / ``arr_len`` / ``obj_keys``:
    ``None`` when the field never held that type; ``gen``: the state gen of the positions;
    ``elapsed``: seconds of the whole call; ``warnings`` / ``warning_params``: i18n keys (texts:
    :meth:`warning_texts`).

    Extensions: ``uncounted`` (container values of streamed records too large to compare: in
    ``types`` / ``values`` / lengths but not in ``distinct`` / ``top``), ``multi`` (a record may
    hold several values: ``count`` and ``records`` differ), ``context``, ``partial`` (a live
    snapshot of :func:`field_stats`'s ``on_partial``); ``number_hist`` / ``length_hist``: the
    histogram material (module docstring, "Histograms"; ``None`` without numbers / strings) behind
    :meth:`histogram` and :meth:`length_histogram` -- not part of :meth:`to_dict`."""

    field: str
    path: tuple
    is_key: bool
    scanned: int
    skipped: int
    base: int
    present: int
    missing: int
    values: int
    nulls: int
    types: dict[str, int]
    distinct: int
    distinct_exact: bool
    distinct_rse: float | None
    top: list[ValueCount]
    top_exact: bool
    top_bound: int
    number: NumberStats | None
    string: StringStats | None
    arr_len: tuple[int, int] | None
    obj_keys: tuple[int, int] | None
    gen: int | None
    elapsed: float
    warnings: tuple[str, ...] = ()
    warning_params: tuple[dict[str, Any], ...] = ()
    uncounted: int = 0
    multi: bool = False
    context: str = "$"
    partial: bool = False
    number_hist: Any = dc_field(default=None, repr=False, compare=False)
    length_hist: Any = dc_field(default=None, repr=False, compare=False)

    @property
    def presence(self) -> float:
        """``present / base`` (0..1; 0 without contexts)."""
        return self.present / self.base if self.base else 0.0

    def warning_texts(self) -> list[str]:
        """The warnings in the current UI language (``<key>.one`` for a count of 1)."""
        return _warning_texts(self.warnings, self.warning_params)

    def to_dict(self) -> dict[str, Any]:
        """JSON-compatible form (numbers of the data as exact texts)."""
        return {
            "field": self.field,
            "path": [s.value if isinstance(s, Wild) else s for s in self.path],
            "key": self.is_key,
            "context": self.context,
            "multi": self.multi,
            "scanned": self.scanned,
            "skipped": self.skipped,
            "base": self.base,
            "present": self.present,
            "missing": self.missing,
            "values": self.values,
            "nulls": self.nulls,
            "types": dict(self.types),
            "distinct": self.distinct,
            "distinct_exact": self.distinct_exact,
            "distinct_rse": self.distinct_rse,
            "top": [v.to_dict() for v in self.top],
            "top_exact": self.top_exact,
            "top_bound": self.top_bound,
            "number": None if self.number is None else self.number.to_dict(),
            "string": None if self.string is None else self.string.to_dict(),
            "arr_len": None if self.arr_len is None else list(self.arr_len),
            "obj_keys": None if self.obj_keys is None else list(self.obj_keys),
            "uncounted": self.uncounted,
            "gen": self.gen,
            "elapsed": self.elapsed,
            "partial": self.partial,
            "warnings": self.warning_texts(),
        }

    def format_text(self, *, records: int | None = None) -> str:
        """A localized plain-text block (the CLI's ``stats`` output, map_stats §6)."""
        return format_stats([self], records=records)

    def histogram(self, bins: int = HIST_BINS) -> Histogram | None:
        """The histogram of the field's finite numbers over ``bins`` nice bins at most (module
        docstring, "Histograms"); ``None`` when the field holds no (finite) number. Exact when the
        distinct numbers fitted the sample, else the reservoir sample's counts scaled to the total."""
        s = self.number_hist
        if s is None or not s.n:
            return None
        return s.histogram(bins)

    def length_histogram(self, bins: int = HIST_BINS) -> Histogram | None:
        """The histogram of the string lengths (code points); ``None`` without strings. Exact up to
        :data:`LEN_COUNTS` distinct lengths, else the lengths are grouped (``exact`` False)."""
        c = self.length_hist
        if c is None or not c.n:
            return None
        return c.histogram(bins)


# ----------------------------------------------------------------------------------
# tokens: canonical keys, types, numbers
# ----------------------------------------------------------------------------------
def _dump(value: Any) -> bytes:
    """``codec.dumps(value)`` as a compact ``bytes``: orjson's result keeps its 4 KiB output buffer
    (``sys.getsizeof`` shows 41 bytes for a small number; tracemalloc ~4 100), and the streamed and
    parsed values are kept as dict keys, cache keys and Misra-Gries tokens -- 4 KiB each made one
    record with millions of values take gigabytes (review R5A-1)."""
    return codec.dumps(value)        # codec.dumps returns a compact copy (it no longer keeps orjson's buffer)


def _digest(canon: bytes) -> bytes:
    h = _H_EMPTY.copy()
    h.update(canon)
    return h.digest()


_NUMBER_PARTS = re.compile(rb"(-?)([0-9]+)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?")
_MAX_DIGITS = 4000           # int() of longer digit strings is refused by Python's default limit
_DEC_INF = Decimal("Infinity")


def _number_info(tok: bytes) -> tuple[Any, bool, int | None, int]:
    """``(value, integral, m, e)`` of a number token: ``value`` compares exactly (``int`` for
    plain integer tokens, else ``Decimal``; +-``Decimal("Infinity")`` / ``0`` for exponents beyond
    the Decimal range), the exact value is ``m * 10**e``. ``m`` = ``None`` when the token does not
    split that way: ``e`` = 0 -- the sum uses ``value`` (more than 4 000 digits); ``e`` = 1 -- the
    number cannot be summed exactly (exponents beyond about 10**15)."""
    if 0x2E not in tok and 0x65 not in tok and 0x45 not in tok and len(tok) <= _MAX_DIGITS:
        v = int(tok)
        return v, True, v, 0
    mt = _NUMBER_PARTS.fullmatch(tok)
    if mt is None:
        raise ValueError(f"invalid JSON number token: {bytes(tok[:40])!r}")
    neg, ip, fp, ep = mt.groups()
    digits = ip + fp if fp else ip
    if len(digits) > _MAX_DIGITS or (ep is not None and len(ep.lstrip(b"+-")) > 15):
        try:
            value = Decimal(tok.decode("ascii"))
        except ArithmeticError:      # an exponent beyond the Decimal range (1e99999999999999999999)
            if ep is not None and ep.startswith(b"-"):
                return Decimal(0), False, None, 1      # (a tiny nonzero value: compared as 0)
            value = -_DEC_INF if neg else _DEC_INF
            return value, True, None, 0
        if abs(value.adjusted()) > 10 ** 15:           # beyond any exact sum: compared, not summed
            return value, _dec_integral(value), None, 1
        return value, _dec_integral(value), None, 0
    e = (int(ep) if ep else 0) - (len(fp) if fp else 0)
    m = int(digits)
    if neg:
        m = -m
    if e >= 0:
        integral = True
    elif not m:
        integral = True
    else:
        z = len(digits) - len(digits.rstrip(b"0"))
        integral = z >= -e
    return Decimal(tok.decode("ascii")), integral, m, e


def _dec_integral(d: Decimal) -> bool:
    if not d.is_finite():
        return False
    return d.as_tuple().exponent >= 0 or d == d.to_integral_value()


def _number_key(canon: bytes) -> bytes | None:
    """The canonical JSON token of a number (``canon`` = dupes' canonical bytes): plain digits
    when the integer fits :data:`LONG_TOKEN` characters, a plain decimal (no trailing zeros) when
    that fits, else ``<d>[.<ddd>]e<exponent>``; ``None`` for absurd exponents."""
    if canon == b"n0":
        return b"0"
    neg = canon[1] == 0x2D
    body = canon[2:-1]
    i = body.index(b"e")          # the first "e": the exponent is hex (may hold "e")
    sig = body[:i]
    hx = body[i + 1:]
    if len(hx) > 24:
        return None
    exp = int(hx, 16)
    nd = len(sig)
    out: bytes | None = None
    if exp >= 0:
        if nd + exp <= LONG_TOKEN:
            out = sig + b"0" * exp
    else:
        point = nd + exp
        # (the length first: b"0" * 10**20 for 1e-99999999999999999999 raised OverflowError)
        if point > 0:
            out = sig[:point] + b"." + sig[point:]
        elif 2 - point + nd <= LONG_TOKEN:
            out = b"0." + b"0" * (-point) + sig
        if out is not None and len(out) > LONG_TOKEN:
            out = None
    if out is None:
        adj = nd - 1 + exp
        out = sig[:1] + (b"." + sig[1:] if nd > 1 else b"") + b"e" + str(adj).encode("ascii")
    return b"-" + out if neg else out


def _analyze(tok: bytes) -> tuple[bytes, int, Any, bytes | None]:
    """``(key, type index, info, canonical bytes or None)`` of a raw JSON token.

    ``key``: the canonical token of the value when it has at most :data:`LONG_TOKEN` bytes (the
    same bytes for every spelling of the value), else ``b"#"`` + the 16-byte blake2b digest of the
    canonical bytes; containers are always hashed. ``info``: the length in code points (strings),
    the number of elements / distinct keys (containers), :func:`_number_info` (numbers), ``None``
    (literals). The canonical bytes are ``None`` for a number that is already canonical (callers
    that hash use :func:`_canon_of`). Errors of :data:`_DECODE_ERRORS` for invalid tokens."""
    c = tok[0]
    if c == 0x22:
        if 0x5C not in tok:
            body = tok[1:-1]
            n = len(body.decode("utf-8"))
            canon = b"s" + body
            if len(tok) <= LONG_TOKEN:
                return tok, T_STRING, n, canon
            return b"#" + _digest(canon), T_STRING, n, canon
        text = tok.decode("utf-8")
        s, end = _scanstring(text, 1)
        if end != len(text):
            raise ValueError("invalid JSON string token")
        canon = b"s" + s.encode("utf-8", "surrogatepass")
        key = _dump(s)
        if len(key) > LONG_TOKEN:
            key = b"#" + _digest(canon)
        return key, T_STRING, len(s), canon
    if c == 0x7B or c == 0x5B:
        v = codec.loads(tok)
        if not isinstance(v, (dict, list)):  # pragma: no cover - loads checks the whole text
            raise ValueError("invalid JSON container token")
        canon = canonical_bytes(v)
        return b"#" + _digest(canon), (T_OBJECT if c == 0x7B else T_ARRAY), len(v), canon
    lit = _LITERALS.get(c)
    if lit is not None:
        if tok != lit[0]:
            raise ValueError("invalid JSON literal")
        return lit[0], lit[1], None, lit[2]
    info = _number_info(tok)
    if len(tok) <= LONG_TOKEN and _quick_canonical(tok):
        return tok, T_NUMBER, info, None
    canon = canonical_token(tok)
    key = _number_key(canon)
    if key is None or len(key) > LONG_TOKEN:
        key = b"#" + _digest(canon)
    return key, T_NUMBER, info, canon


def _canon_of(key: bytes, canon: bytes | None) -> bytes:
    """The canonical bytes of a value (``canon`` from :func:`_analyze`, else from its token)."""
    return canon if canon is not None else canonical_token(key)


def _token_hash(tok: bytes) -> int:
    """``dupes.key_hash(canonical_token(tok))`` (fast for strings without escapes)."""
    if tok[0] == 0x22 and 0x5C not in tok:
        h = _H_STRING.copy()
        h.update(tok[1:-1])
        return _from_bytes(h.digest(), "big")
    return key_hash(canonical_token(tok))


def _quick_canonical(tok: bytes) -> bool:
    """A short raw token that is the canonical token of its value (no normalization needed):
    strings without escapes, literals, numbers without exponent whose fraction does not end in
    ``0`` (and not ``-0``)."""
    c = tok[0]
    if c == 0x22:
        return 0x5C not in tok
    if c == 0x7B or c == 0x5B:
        return False
    if c in _LITERALS:
        return True
    if 0x65 in tok or 0x45 in tok:
        return False
    if 0x2E in tok:
        return tok[-1] != 0x30
    return tok != b"-0"


def _key_str_len(key: bytes) -> int:
    """Length in code points of a string token (a dict key; escapes decoded when present)."""
    if 0x5C not in key:
        body = key[1:-1]
        return len(body) if body.isascii() else len(body.decode("utf-8", "replace"))
    return _analyze(key)[2]


def _type_of(key: bytes) -> int:
    """Type index of a token by its first byte."""
    c = key[0]
    if c == 0x22:
        return T_STRING
    if c == 0x7B:
        return T_OBJECT
    if c == 0x5B:
        return T_ARRAY
    if c == 0x74 or c == 0x66:
        return T_BOOL
    if c == 0x6E:
        return T_NULL
    return T_NUMBER


def _u64(data: bytes | bytearray | memoryview) -> array:
    a = array("Q")
    a.frombytes(data)
    return a


def _quads(data: bytes | bytearray | memoryview) -> Any:
    """The extractor's quads without a copy (a ``Q`` view; 32 bytes per value: a copy doubled
    the peak of a record with millions of values -- review R5A-1)."""
    mv = memoryview(data)
    return mv.cast("Q") if len(mv) else array("Q")


def _unpack(v: int, multi: bool) -> tuple[int, int, int, int, int]:
    """(count, records, first ordinal, first index inside that record, last ordinal) of a
    packed dict entry (single-valued: ``first << 40 | count``; multi-valued: ``first << 144 |
    inrec << 120 | last << 80 | records << 40 | count``)."""
    if multi:
        return v & _M40, (v >> 40) & _M40, v >> 144, (v >> 120) & _M24, (v >> 80) & _M40
    c = v & _M40
    first = v >> 40
    return c, c, first, 0, first


def _pack(multi: bool, count: int, records: int, first: int, inrec: int, last: int) -> int:
    if multi:
        return (first << 144) | (min(inrec, _M24) << 120) | (last << 80) | (records << 40) | count
    return (first << 40) | count


def _spill_entries(out: list[int], h96: int, first: int, inrec: int, count: int, records: int,
                   flag: int = 0) -> None:
    """Aggregated run entries of one value: ``hash96 | first (40 bits) | inrec (15) | flag (1) |
    count (20) | records (20)``, counts split over several entries when needed. ``flag`` = 1: an
    entry of a single-record accumulator (``records`` 0; the walk counts the record once)."""
    head = (h96 << 96) | (first << 56) | (min(inrec, _M15) << 41) | (flag << 40)
    while count > 0:
        c = count if count <= _M20 else _M20
        r = records if records <= c else c
        out.append(head | (c << 20) | r)
        count -= c
        records -= r


# ----------------------------------------------------------------------------------
# aggregates: types, exact numbers, lengths
# ----------------------------------------------------------------------------------
class _Sum:
    """The exact sum (module docstring): integers in ``ints``, other numbers ``m * 10**e`` as
    exact integer sums per exponent ``e`` (``parts``), and the few tokens that do not split that
    way (more than 4 000 digits) as Decimals within the digit guard; ``exact`` turns False when
    the guard tripped (the sum is then given with 50 significant digits); ``skipped`` counts the
    numbers left out of the sum (and of the mean): exponents beyond about ``10**15`` or beyond the
    Decimal range (``1e99999999999999999999``) -- they no longer make the rest of the sum
    approximate (review R5A-11)."""

    __slots__ = ("ints", "parts", "dec", "exact", "min_exp", "skipped", "scale")

    def __init__(self) -> None:
        self.ints = 0
        self.parts: dict[int, int] = {}
        self.dec: Decimal | None = None
        self.exact = True
        self.min_exp = 0
        self.skipped = 0
        # the finest exponent of every number's spelling (``m * 10**e``): the scale of the sum's
        # text, the same whatever spellings the dicts collapse (``1.100`` keyed as ``1.1``) or the
        # sketches see one by one -- the sum of 1.10 and 2.20 is 3.30 in every mode
        self.scale: int | None = None

    def note(self, e: int) -> None:
        """A spelling of exponent ``e`` was added (in full or as another spelling of a value)."""
        if self.scale is None or e < self.scale:
            self.scale = e

    def add(self, info: tuple[Any, bool, int | None, int], count: int) -> None:
        value, _integral, m, e = info
        if m is not None:
            if self.scale is None or e < self.scale:
                self.scale = e
            if e == 0:
                self.ints += m * count
            else:
                parts = self.parts
                parts[e] = parts.get(e, 0) + m * count
            return
        if e or not value.is_finite():
            self.skipped += count   # beyond any exact sum: left out of the sum and the mean
            return
        self._add_dec(value if count == 1 else _EXACT.multiply(value, Decimal(count)))

    def _add_dec(self, term: Decimal) -> None:
        dec = self.dec
        texp = term.as_tuple().exponent
        texp = texp if isinstance(texp, int) else 0
        if dec is None:
            self.dec = term
            self.min_exp = min(0, texp)
            return
        if self.exact:
            lo = min(self.min_exp, texp)
            hi = max(dec.adjusted(), term.adjusted())
            if hi - lo + 1 > SUM_DIGITS_MAX:
                self.exact = False
                self.dec = _APPROX.add(dec, term)
            else:
                self.dec = _EXACT.add(dec, term)
                self.min_exp = lo
        else:
            self.dec = _APPROX.add(dec, term)

    def merge(self, other: _Sum) -> None:
        self.ints += other.ints
        parts = self.parts
        for e, v in other.parts.items():
            parts[e] = parts.get(e, 0) + v
        if not other.exact:
            self.exact = False
        self.skipped += other.skipped
        if other.scale is not None:
            self.note(other.scale)
        if other.dec is not None:
            self._add_dec(other.dec)

    def total(self) -> tuple[Decimal, bool]:
        """(the sum of the numbers not left out, exact: not rounded)."""
        terms = [(e, v) for e, v in self.parts.items() if v]
        if self.ints:
            terms.append((0, self.ints))
        exact = self.exact
        if terms:
            lo = min(e for e, _ in terms)
            if self.scale is not None and self.scale < lo:
                lo = self.scale            # the finest spelling's scale (``scale``), also when its terms cancel
            hi = max(e + _digits(v) - 1 for e, v in terms)
            if exact and hi - lo + 1 <= SUM_DIGITS_MAX:
                n = 0
                for e, v in terms:
                    n += v * 10 ** (e - lo)
                part = Decimal(n).scaleb(lo, context=_EXACT)
            else:
                exact = False
                part = Decimal(0)
                for e, v in sorted(terms):
                    part = _APPROX.add(part, Decimal(v).scaleb(e, context=_EXACT))
        else:
            part = Decimal(0)
        if self.dec is None:
            return part, exact
        if not terms:
            return self.dec, exact
        if exact:
            pexp = part.as_tuple().exponent
            pexp = pexp if isinstance(pexp, int) else 0
            hi = max(part.adjusted(), self.dec.adjusted())
            if hi - min(self.min_exp, pexp) + 1 <= SUM_DIGITS_MAX:
                return _EXACT.add(part, self.dec), True
        return _APPROX.add(part, self.dec), False


def _digits(n: int) -> int:
    """Decimal digits of ``|n|`` (estimated from the bit length beyond ~4 000 digits, where
    ``str()`` is refused: the guard only needs the order of magnitude)."""
    n = abs(n)
    if n.bit_length() <= 13000:
        return len(str(n)) if n else 1
    return int(n.bit_length() * 0.30102999566398) + 1


def _seed_of(text: str) -> int:
    """A deterministic seed of the field text (blake2b: the same in every process)."""
    return _from_bytes(hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=8).digest(), "big")


def _hist_k(budget: int, nfields: int) -> int:
    """Numbers kept per field for the histogram (module docstring, "Histograms")."""
    share = (max(1, int(budget)) // HIST_SHARE) // (max(1, int(nfields)) * HIST_ENTRY_COST)
    return max(HIST_MIN, min(HIST_SAMPLE, share))


def _skip(rng: random.Random, logw: float) -> int:
    """Algorithm L: the items rejected before the next accepted one, for the threshold ``W =
    exp(logw)`` (an item is accepted with probability ``W``): ``floor(log(u) / log(1 - W))``."""
    w = math.exp(logw)
    if w >= 1.0:
        return 0
    lw = math.log1p(-w)
    if lw == 0.0:                        # W underflowed: no item is accepted any more
        return 1 << 62
    s = math.log(1.0 - rng.random()) / lw
    return int(s) if s < 4e18 else 1 << 62


class _Sample:
    """The numbers of a field for its histogram (module docstring, "Histograms"): the exact ``value ->
    count`` of up to ``k`` distinct finite values (``d``), then a uniform reservoir sample of ``k``
    occurrences (``items``; Li's Algorithm L -- ``_logw`` is the log of the acceptance threshold,
    ``_next`` the index of the next accepted occurrence -- with weighted insertions: :meth:`add` stands
    for ``count`` occurrences). ``n``: finite occurrences seen; ``lo`` / ``hi``: their exact extremes;
    ``integral``: every one of them was integral; ``skipped``: values beyond the Decimal range, left
    out. Values are ``int`` or ``Decimal`` (``1 == Decimal("1.0")``: one key)."""

    __slots__ = ("k", "rng", "d", "items", "n", "lo", "hi", "integral", "skipped", "_logw", "_next")

    def __init__(self, k: int = HIST_SAMPLE, seed: str = "") -> None:
        self.k = max(1, int(k))
        self.rng = random.Random(_seed_of(seed))
        self.d: dict[Any, int] | None = {}
        self.items: list[Any] | None = None
        self.n = 0
        self.lo: Any = None
        self.hi: Any = None
        self.integral = True
        self.skipped = 0
        self._logw = 0.0
        self._next = 0

    @property
    def exact(self) -> bool:
        return self.d is not None

    @property
    def size(self) -> int:
        """Entries kept (distinct values, or sampled occurrences)."""
        return len(self.d) if self.d is not None else len(self.items or ())

    @property
    def nbytes(self) -> int:
        return self.size * HIST_ENTRY_COST

    def copy(self) -> _Sample:
        c = _Sample.__new__(_Sample)
        c.k = self.k
        c.rng = random.Random()
        c.rng.setstate(self.rng.getstate())
        c.d = dict(self.d) if self.d is not None else None
        c.items = list(self.items) if self.items is not None else None
        c.n, c.lo, c.hi, c.integral, c.skipped = self.n, self.lo, self.hi, self.integral, self.skipped
        c._logw, c._next = self._logw, self._next
        return c

    # -- adding ------------------------------------------------------------------------------
    def add(self, value: Any, count: int = 1, integral: bool = True) -> None:
        """``count`` occurrences of ``value`` (``int`` / ``Decimal``; non-finite ones are skipped)."""
        if count <= 0:
            return
        if isinstance(value, Decimal) and not value.is_finite():
            self.skipped += count
            return
        if not integral:
            self.integral = False
        if self.n == 0:
            self.lo = self.hi = value
        elif value < self.lo:
            self.lo = value
        elif value > self.hi:
            self.hi = value
        self._put(value, count)

    def _put(self, value: Any, count: int) -> None:
        d = self.d
        if d is not None:
            c = d.get(value)
            if c is not None:
                d[value] = c + count
                self.n += count
                return
            if len(d) < self.k:
                d[value] = count
                self.n += count
                return
            self._to_reservoir()
        self._reservoir_add(value, count)

    def _to_reservoir(self) -> None:
        """The exact dict becomes a uniform ``k``-sample of its ``n`` occurrences."""
        d = self.d
        assert d is not None
        if self.n <= self.k:
            items: list[Any] = []
            for v, c in d.items():
                items.extend([v] * c)
        else:
            items = self._draw_dict(d, self.n, self.k)
        self.d = None
        self.items = items
        self._restart()

    def _draw_dict(self, d: dict[Any, int], n: int, m: int) -> list[Any]:
        """``m`` occurrences drawn uniformly without replacement from the ``n`` of ``d``."""
        out: list[Any] = []
        if m <= 0:
            return out
        positions = iter(sorted(self.rng.sample(range(n), m)))
        want = next(positions, None)
        cum = 0
        for v, c in d.items():
            cum += c
            while want is not None and want < cum:
                out.append(v)
                want = next(positions, None)
            if want is None:
                break
        return out

    def _restart(self) -> None:
        """Re-seat Algorithm L after ``n`` occurrences with a full reservoir: the threshold is the
        ``k``-th smallest of ``n`` uniform keys, ``Beta(k, n - k + 1)``."""
        n, k = self.n, self.k
        w = self.rng.betavariate(k, max(1, n - k + 1))
        self._logw = math.log(w) if 0.0 < w < 1.0 else (-1e-300 if w >= 1.0 else -700.0)
        self._next = n + _skip(self.rng, self._logw)

    def _reservoir_add(self, value: Any, count: int) -> None:
        items = self.items
        assert items is not None
        k = self.k
        n = self.n
        if len(items) < k:                      # not full yet: the occurrences fill it
            take = min(count, k - len(items))
            items.extend([value] * take)
            n += take
            count -= take
            self.n = n
            if len(items) == k:
                self._restart()
            if count <= 0:
                return
        end = n + count
        nxt = self._next
        if nxt < end:
            rng = self.rng
            logw = self._logw
            while nxt < end:
                items[rng.randrange(k)] = value
                logw += math.log(1.0 - rng.random()) / k       # W *= u ** (1 / k)
                nxt += _skip(rng, logw) + 1
            self._logw = logw
            self._next = nxt
        self.n = end

    # -- merging -----------------------------------------------------------------------------
    def merge(self, other: _Sample) -> None:
        """The sample of the union (``other``: the same field's sample of other occurrences)."""
        self.skipped += other.skipped
        if other.n == 0:
            return
        if not other.integral:
            self.integral = False
        if self.n == 0:
            self.lo, self.hi, self.n = other.lo, other.hi, other.n
            if other.d is not None:
                self.d = dict(other.d)
                self.items = None
            else:
                self.d = None
                self.items = list(other.items or ())
                self._restart()
            return
        if other.lo < self.lo:
            self.lo = other.lo
        if other.hi > self.hi:
            self.hi = other.hi
        if other.d is not None:                 # its occurrences are known: fed as a stream
            for v, c in other.d.items():
                self._put(v, c)
            return
        # other is a reservoir (it saw at least k occurrences): a uniform k-subset of the union, each
        # side giving as many of its occurrences as the draw puts on its side
        na = self.n
        total = na + other.n
        picks = self.rng.sample(range(total), min(self.k, total))
        mb = sum(1 for i in picks if i >= na)
        ma = len(picks) - mb
        if self.d is not None:
            a_items = self._draw_dict(self.d, na, ma)
        else:
            a_items = self.rng.sample(self.items or [], min(ma, len(self.items or [])))
        b_items = self.rng.sample(other.items or [], min(mb, len(other.items or [])))
        self.d = None
        self.items = a_items + b_items
        self.n = total
        self._restart()

    # -- the histogram -----------------------------------------------------------------------
    def histogram(self, bins: int = HIST_BINS, lo: Any = None, hi: Any = None) -> Histogram | None:
        """The histogram of the occurrences seen (``lo`` / ``hi``: other exact extremes to cover)."""
        if not self.n:
            return None
        lo = self.lo if lo is None else min(lo, self.lo)
        hi = self.hi if hi is None else max(hi, self.hi)
        edges = nice_edges(lo, hi, bins, self.integral)
        nb = len(edges) - 1
        counts = [0] * nb
        last = nb - 1
        if self.d is not None:
            pairs: Iterable[tuple[Any, int]] = self.d.items()
            m = self.n
        else:
            pairs = ((v, 1) for v in self.items or ())
            m = len(self.items or ())
        br = bisect.bisect_right
        for v, c in pairs:
            i = br(edges, v) - 1
            counts[last if i > last else (0 if i < 0 else i)] += c
        if self.d is None and m and m != self.n:
            counts = [round(c * self.n / m) for c in counts]
        return Histogram(edges, counts, self.d is not None or m == self.n, m)


class _LenCounts:
    """Exact counts per string length (code points) up to :data:`LEN_COUNTS` distinct lengths; beyond,
    the lengths are grouped by a doubling ``width`` (key ``length // width``)."""

    __slots__ = ("d", "width", "n")

    def __init__(self) -> None:
        self.d: dict[int, int] = {}
        self.width = 1
        self.n = 0

    @property
    def nbytes(self) -> int:
        return len(self.d) * ENTRY_COST

    def copy(self) -> _LenCounts:
        c = _LenCounts()
        c.d = dict(self.d)
        c.width = self.width
        c.n = self.n
        return c

    def add(self, length: int, count: int = 1) -> None:
        d = self.d
        key = length if self.width == 1 else length // self.width
        d[key] = d.get(key, 0) + count
        self.n += count
        if len(d) > LEN_COUNTS:
            self._coarsen()

    def _rekey(self, width: int) -> None:
        if width <= self.width:
            return
        f = width // self.width
        nd: dict[int, int] = {}
        for key, c in self.d.items():
            k2 = key // f
            nd[k2] = nd.get(k2, 0) + c
        self.d = nd
        self.width = width

    def _coarsen(self) -> None:
        while len(self.d) > LEN_COUNTS:
            self._rekey(self.width * 2)

    def merge(self, other: _LenCounts) -> None:
        if not other.n:
            return
        if other.width > self.width:
            self._rekey(other.width)
        if other.width < self.width:
            other = other.copy()
            other._rekey(self.width)
        d = self.d
        for key, c in other.d.items():
            d[key] = d.get(key, 0) + c
        self.n += other.n
        if len(d) > LEN_COUNTS:
            self._coarsen()

    def histogram(self, bins: int = HIST_BINS) -> Histogram | None:
        if not self.n:
            return None
        w = self.width
        lo = min(self.d) * w
        hi = max(self.d) * w + (w - 1)
        edges = nice_edges(lo, hi, bins, True)
        counts = [0] * (len(edges) - 1)
        last = len(counts) - 1
        br = bisect.bisect_right
        for key, c in self.d.items():
            i = br(edges, key * w) - 1
            counts[last if i > last else (0 if i < 0 else i)] += c
        return Histogram(edges, counts, w == 1, self.n)


class _Agg:
    """Statistics that do not need the distinct values: occurrences per type, integral numbers,
    the exact sum, number extremes (value, first occurrence, token text), string / array / object
    lengths, empty strings; the histogram material (``hist``: numbers, ``lens``: string lengths)."""

    __slots__ = ("types", "integers", "sum", "nmin", "nmax", "slo", "shi", "empty", "alo", "ahi", "klo", "khi",
                 "hist", "lens")

    def __init__(self, hist_k: int = HIST_SAMPLE, seed: str = "") -> None:
        self.types = [0, 0, 0, 0, 0, 0]
        self.integers = 0
        self.sum = _Sum()
        self.nmin: list[Any] | None = None     # [value, first ordinal, inrec, token text]
        self.nmax: list[Any] | None = None
        self.slo = self.shi = -1
        self.empty = 0
        self.alo = self.ahi = -1
        self.klo = self.khi = -1
        self.hist = _Sample(hist_k, seed)
        self.lens = _LenCounts()

    @property
    def nbytes(self) -> int:
        """Accounted bytes of the histogram material."""
        return self.hist.nbytes + self.lens.nbytes

    def add(self, tidx: int, info: Any, count: int, first: int, inrec: int, text: bytes) -> None:
        """``count`` occurrences of one value (``text``: a token of it, for number extremes)."""
        self.types[tidx] += count
        if tidx == T_STRING:
            n = info
            if self.shi < 0:
                self.slo = self.shi = n
            elif n < self.slo:
                self.slo = n
            elif n > self.shi:
                self.shi = n
            if n == 0:
                self.empty += count
            self.lens.add(n, count)
        elif tidx == T_NUMBER:
            if info[1]:
                self.integers += count
            self.sum.add(info, count)
            self._extreme(info[0], first, inrec, text)
            self.hist.add(info[0], count, info[1])
        elif tidx == T_ARRAY:
            self.add_len(False, info)
        elif tidx == T_OBJECT:
            self.add_len(True, info)

    def _extreme(self, value: Any, first: int, inrec: int, text: Any) -> None:
        m = self.nmin
        if m is None:
            self.nmin = [value, first, inrec, text]
            self.nmax = [value, first, inrec, text]
            return
        if value < m[0] or (value == m[0] and (first, inrec) < (m[1], m[2])):
            self.nmin = [value, first, inrec, text]
        m = self.nmax
        if value > m[0] or (value == m[0] and (first, inrec) < (m[1], m[2])):   # type: ignore[index]
            self.nmax = [value, first, inrec, text]

    def add_len(self, is_map: bool, n: int) -> None:
        if is_map:
            if self.khi < 0:
                self.klo = self.khi = n
            elif n < self.klo:
                self.klo = n
            elif n > self.khi:
                self.khi = n
        else:
            if self.ahi < 0:
                self.alo = self.ahi = n
            elif n < self.alo:
                self.alo = n
            elif n > self.ahi:
                self.ahi = n

    def merge(self, o: _Agg) -> None:
        for i in range(6):
            self.types[i] += o.types[i]
        self.integers += o.integers
        self.sum.merge(o.sum)
        self.hist.merge(o.hist)
        self.lens.merge(o.lens)
        for m in (o.nmin, o.nmax):
            if m is not None:
                self._extreme(m[0], m[1], m[2], m[3])
        if o.shi >= 0:
            for n in (o.slo, o.shi):
                if self.shi < 0:
                    self.slo = self.shi = n
                else:
                    self.slo = min(self.slo, n)
                    self.shi = max(self.shi, n)
        self.empty += o.empty
        if o.ahi >= 0:
            self.add_len(False, o.alo)
            self.add_len(False, o.ahi)
        if o.khi >= 0:
            self.add_len(True, o.klo)
            self.add_len(True, o.khi)


# ----------------------------------------------------------------------------------
# sketches
# ----------------------------------------------------------------------------------
_HLL_Q = 64 - HLL_P                    # hash bits below the register index: ranks 1 .. Q + 1
_HLL_LOW = (1 << _HLL_Q) - 1
_HLL_ALPHA_INF = 1.0 / (2.0 * math.log(2.0))
HLL_BYTES = HLL_M + 100                # accounted bytes of one HyperLogLog


def _ertl_sigma(x: float) -> float:
    """sigma(x) of Ertl's improved estimator (the registers still 0)."""
    if x == 1.0:
        return math.inf
    y = 1.0
    z = x
    while True:
        x *= x
        z_old = z
        z += x * y
        y += y
        if z == z_old:
            return z


def _ertl_tau(x: float) -> float:
    """tau(x) of Ertl's improved estimator (the registers at the maximum rank)."""
    if x == 0.0 or x == 1.0:
        return 0.0
    y = 1.0
    z = 1.0 - x
    while True:
        x = math.sqrt(x)
        z_old = z
        y *= 0.5
        z -= (1.0 - x) ** 2 * y
        if z == z_old:
            return z / 3.0


class _HLL:
    """HyperLogLog, p = 14 (module docstring), estimated with Ertl's improved estimator (O. Ertl,
    "New cardinality estimation algorithms for HyperLogLog sketches", 2017): unbiased over the
    whole range -- the classic estimator with a switch to linear counting below ``2.5 m`` was
    biased by +1.4 .. +2.9 % just above the switch (review R5A-6)."""

    __slots__ = ("reg",)

    def __init__(self) -> None:
        self.reg = bytearray(HLL_M)

    def add(self, h64: int) -> None:
        reg = self.reg
        idx = h64 >> _HLL_Q
        rank = (_HLL_Q + 1) - (h64 & _HLL_LOW).bit_length()
        if rank > reg[idx]:
            reg[idx] = rank

    def merge(self, other: _HLL) -> None:
        self.reg = bytearray(map(max, self.reg, other.reg))

    def estimate(self) -> float:
        reg = self.reg
        m = HLL_M
        z = m * _ertl_tau((m - reg.count(_HLL_Q + 1)) / m)
        for k in range(_HLL_Q, 0, -1):
            z = 0.5 * (z + reg.count(k))
        z += m * _ertl_sigma(reg.count(0) / m)
        return _HLL_ALPHA_INF * m * m / z


class _MG:
    """Misra-Gries summary with batch decrement: at most ``2k`` counters, pruned to the ``k``
    largest by subtracting the ``(k+1)``-th largest count ``d`` from every counter (``D += d``).
    Counter: ``[count, records, first, inrec, last, display token]``."""

    __slots__ = ("c", "k", "d")

    def __init__(self, k: int = MG_COUNTERS) -> None:
        self.c: dict[int, list[Any]] = {}
        self.k = max(1, int(k))
        self.d = 0

    def add(self, h: int, o: int, inrec: int, disp: bytes | None) -> None:
        e = self.c.get(h)
        if e is None:
            self.c[h] = [1, 1, o, inrec, o, disp]
            if len(self.c) > 2 * self.k:
                self.prune()
        else:
            e[0] += 1
            if e[4] != o:
                e[4] = o
                e[1] += 1

    def add_weighted(self, h: int, count: int, records: int, first: int, inrec: int, last: int,
                     disp: bytes | None) -> None:
        e = self.c.get(h)
        if e is None:
            self.c[h] = [count, records, first, inrec, last, disp]
            if len(self.c) > 2 * self.k:
                self.prune()
            return
        e[0] += count
        if e[4] != last:
            e[1] += records
            if last > e[4]:
                e[4] = last
        if (first, inrec) < (e[2], e[3]):
            e[2] = first
            e[3] = inrec
            if disp is not None:
                e[5] = disp

    def prune(self) -> None:
        c = self.c
        if len(c) <= self.k:
            return
        d = heapq.nlargest(self.k + 1, (e[0] for e in c.values()))[-1]
        self.d += d
        kept: dict[int, list[Any]] = {}
        for h, e in c.items():
            n = e[0] - d
            if n > 0:
                e[0] = n
                e[1] = max(1, e[1] - d)
                kept[h] = e
        self.c = kept

    def merge(self, other: _MG) -> None:
        for h, e in other.c.items():
            self.add_weighted(h, *e)
        self.d += other.d
        self.prune()

    def resize(self, k: int) -> None:
        """Fewer counters: prune to the ``k`` largest (``D`` grows by what is subtracted, so every
        kept count still satisfies ``c <= true count <= c + D``)."""
        if k < self.k:
            self.k = max(1, int(k))
            self.prune()


# ----------------------------------------------------------------------------------
# one field's accumulator
# ----------------------------------------------------------------------------------
class _AccSet:
    """The accumulators sharing one budget (the call's fields, or the fields of one streamed
    record), the temporary runs they create, and the overflow rule."""

    def __init__(self, budget: int, exact: bool, directory: Any, cancel: CancelToken | None,
                 registry: list[Any], single: bool = False, top: int = DEFAULT_TOP,
                 hist_k: int = HIST_SAMPLE) -> None:
        self.single = single              # the accumulators of ONE streamed record
        self.budget = max(1, int(budget))
        self.exact = exact
        self.directory = directory
        self.cancel = cancel
        self.registry = registry          # every HashRuns made (removed at the end of the call)
        self.top = top
        self.hist_k = max(1, int(hist_k))  # numbers kept per field for the histogram (module docstring)
        self.accs: list[_FieldAcc] = []
        self.ticks = 0
        self.overflows = 0
        # token analyses shared by the fields (they do not depend on the field), charged to the
        # budget and capped at an eighth of it (module docstring, "Budget")
        self.ncache: dict[bytes, tuple[bytes, int, Any, bytes]] = {}
        self.scache: dict[bytes, tuple[int, int, Any]] = {}
        self.cache_bytes = 0
        self.cache_cap = max(1, self.budget // 8)

    def new_runs(self) -> Any:
        runs = HashRuns(self.directory, self.cancel)
        self.registry.append(runs)
        return runs

    def tick(self) -> None:
        self.ticks += 1
        if self.ticks >= TICK_VALUES:
            self.ticks = 0
            check(self.cancel)
            self.check_budget()

    def cache_put(self, cache: dict[bytes, Any], tok: bytes, value: Any) -> None:
        if len(cache) >= CACHE_MAX or self.cache_bytes > self.cache_cap:
            self.clear_caches()
        cache[tok] = value
        self.cache_bytes += 2 * len(tok) + CACHE_ENTRY_COST

    def clear_caches(self) -> None:
        self.ncache = {}
        self.scache = {}
        self.cache_bytes = 0

    def dict_bytes(self) -> int:
        """Accounted bytes of the exact dicts (what the budget limits)."""
        n = 0
        for a in self.accs:
            n += a.nbytes
        return n

    def sketch_bytes(self) -> int:
        n = 0
        for a in self.accs:
            n += a.sketch_memory()
        return n

    def hist_bytes(self) -> int:
        """Accounted bytes of the histogram material (bounded by construction: at most ``hist_k``
        numbers and :data:`LEN_COUNTS` lengths per field; not part of the sketch bytes)."""
        n = 0
        for a in self.accs:
            n += a.agg.nbytes
        return n

    def total(self) -> int:
        """Accounted bytes of everything: the dicts, the sketches, the histogram material and the caches."""
        return self.dict_bytes() + self.sketch_bytes() + self.hist_bytes() + self.cache_bytes

    def over(self) -> bool:
        return self.dict_bytes() > self.budget

    def mg_counters(self, extra: int = 0) -> int:
        """Misra-Gries counters ``k`` per sketched field: half the budget shared by the sketched
        fields (``extra``: fields about to be sketched), between ``max(MG_MIN, 2 * top)`` and
        :data:`MG_COUNTERS` (a summary holds up to ``2k`` counters)."""
        sketched = extra + sum(1 for a in self.accs if a.mode == _SKETCH)
        k = (self.budget // 2) // (max(1, sketched) * 2 * MG_ENTRY_COST)
        return max(MG_MIN, 2 * self.top, min(MG_COUNTERS, k))

    def check_budget(self, open_acc: _FieldAcc | None = None, open_record: int = -1) -> None:
        """Move the largest exact dicts out of memory until the rest fits the budget; give the
        sketches fewer counters when together they hold more than half of it (module docstring,
        "Budget"). ``open_acc`` is in the middle of record ``open_record`` (its spill marks that
        record)."""
        total = self.dict_bytes()
        while total > self.budget:
            best = None
            for a in self.accs:
                if a.mode != _SKETCH and a.nbytes > 0 and (best is None or a.nbytes > best.nbytes):
                    best = a
            if best is None:
                break
            self.overflows += 1
            total -= best.nbytes
            best.overflow(open_record if best is open_acc else -1)
        if self.sketch_bytes() > self.budget // 2:
            k = self.mg_counters()
            for a in self.accs:
                if a.mode == _SKETCH and a.mg is not None and a.mg.k > k:
                    a.mg.resize(k)


class _FieldAcc:
    """Distinct values, counts and aggregates of one field (module docstring, "Algorithm")."""

    __slots__ = ("multi", "set", "mode", "d", "spell", "nbytes", "agg", "hll", "mg", "runs", "uncounted", "cost",
                 "split", "snap_hll", "snap_mg", "snap_last")

    def __init__(self, multi: bool, accset: _AccSet, seed: str = "") -> None:
        self.multi = multi
        self.set = accset
        accset.accs.append(self)
        self.mode = _DICT
        self.d: dict[bytes, int] = {}
        self.spell: dict[bytes, bytes] = {}     # key -> the raw spelling of its first occurrence (when different)
        self.nbytes = 0
        self.agg = _Agg(accset.hist_k, seed)   # (seed: the field text -- reproducible histogram samples)
        self.hll: _HLL | None = None
        self.mg: _MG | None = None
        self.runs: Any = None                   # HashRuns of the spilled epochs (exact mode)
        self.uncounted = 0
        self.cost = ENTRY_COST + (MULTI_EXTRA if multi else 0)
        self.split = -1                         # the record the current epoch began inside (-1: none)
        self.snap_hll: _HLL | None = None       # spilled epochs, for the live snapshots only
        self.snap_mg: _MG | None = None
        self.snap_last = 0                      # the distinct count of the last snapshot

    def sketch_memory(self) -> int:
        """Accounted bytes of this field's sketches (and of the live-snapshot summary)."""
        n = 0
        if self.mg is not None:
            n += HLL_BYTES + len(self.mg.c) * MG_ENTRY_COST
        if self.snap_mg is not None:
            n += HLL_BYTES + len(self.snap_mg.c) * MG_ENTRY_COST
        return n

    # -- adding values ---------------------------------------------------------------------
    def _norm(self, tok: bytes) -> tuple[bytes, int, Any, bytes]:
        s = self.set
        hit = s.ncache.get(tok)
        if hit is None:
            hit = _analyze(tok)
            if len(tok) <= CACHE_TOKEN_MAX:
                s.cache_put(s.ncache, tok, hit)
        return hit

    def add(self, tok: bytes, o: int, inrec: int) -> None:
        """One occurrence of the value ``tok`` (a raw JSON token) in the record with ordinal
        ``o`` (``inrec``: its index among the field's values in that record)."""
        if self.mode == _SKETCH:
            self.sketch_add(tok, o, inrec)
        elif self.multi:
            self.add_multi(tok, o, inrec)
        else:
            self.add_single(tok, o)

    def add_single(self, tok: bytes, o: int) -> None:
        key = tok
        if len(tok) > LONG_TOKEN:
            key, tidx, info, _canon = self._norm(tok)
            if key[0] == 0x23:
                self.agg.add(tidx, info, 1, o, 0, tok)
            elif tidx == T_NUMBER and info[2] is not None:
                self.agg.sum.note(info[3])     # keyed by the value's canonical token: keep this spelling's scale
        d = self.d
        v = d.get(key)
        if v is None:
            d[key] = (o << 40) | 1
            self.nbytes += len(key) + self.cost
            if key is not tok and key[0] != 0x23 and key != tok:
                self.spell[key] = tok          # a long spelling of a value with a short canonical token
                self.nbytes += len(tok) + ENTRY_COST
        else:
            d[key] = v + 1

    def add_multi(self, tok: bytes, o: int, inrec: int) -> None:
        if len(tok) <= LONG_TOKEN and _quick_canonical(tok):
            key = tok
        else:
            key, tidx, info, _canon = self._norm(tok)
            if key[0] == 0x23:
                self.agg.add(tidx, info, 1, o, inrec, tok)
            elif tidx == T_NUMBER and info[2] is not None:
                self.agg.sum.note(info[3])     # keyed by the value's canonical token: keep this spelling's scale
        d = self.d
        v = d.get(key)
        if v is None:
            d[key] = (o << 144) | (min(inrec, _M24) << 120) | (o << 80) | _REC_INC
            self.nbytes += len(key) + self.cost
            if key != tok and len(tok) <= max(LONG_TOKEN, 4 * KiB):
                self.spell[key] = tok
                self.nbytes += len(tok) + ENTRY_COST
        else:
            last = (v >> 80) & _M40
            if last == o:
                d[key] = v + 1
            else:
                d[key] = v + ((o - last) << 80) + _REC_INC

    def sketch_add(self, tok: bytes, o: int, inrec: int) -> None:
        s = self.set
        hit = s.scache.get(tok)
        if hit is None:
            key, tidx, info, canon = _analyze(tok)
            h = _from_bytes(key[1:], "big") if key[0] == 0x23 else key_hash(_canon_of(key, canon))
            hit = (h, tidx, info)
            if len(tok) <= CACHE_TOKEN_MAX:
                s.cache_put(s.scache, tok, hit)
        h, tidx, info = hit
        self.agg.add(tidx, info, 1, o, inrec, tok)
        self.hll.add(h & _M64)   # type: ignore[union-attr]
        self.mg.add(h, o, inrec, tok if len(tok) <= LONG_TOKEN else None)   # type: ignore[union-attr]

    def add_entry(self, key: bytes, count: int, records: int, first: int, inrec: int, last: int,
                  spelling: bytes | None) -> None:
        """An aggregated entry of another accumulator of the same field (dict / spill mode)."""
        d = self.d
        v = d.get(key)
        multi = self.multi
        if v is None:
            d[key] = _pack(multi, count, records, first, inrec, last)
            self.nbytes += len(key) + self.cost
            if spelling is not None:
                self.spell[key] = spelling
                self.nbytes += len(spelling) + ENTRY_COST
            return
        c0, r0, f0, i0, l0 = _unpack(v, multi)
        if (first, inrec) < (f0, i0):
            f0, i0 = first, inrec
            if spelling is not None:
                self.spell[key] = spelling
            else:
                self.spell.pop(key, None)
        d[key] = _pack(multi, c0 + count, r0 + (records if last != l0 else 0), f0, i0, max(l0, last))

    # -- leaving the exact phase -----------------------------------------------------------
    def overflow(self, open_record: int = -1) -> None:
        """Leave the exact phase (``open_record``: the record whose values are being added)."""
        if self.set.exact:
            self.spill(open_record)
        else:
            self.to_sketch()

    def _fold(self, key: bytes, v: int, agg: _Agg) -> tuple[int, int, int, int, int, int, bytes | None]:
        """Unpack an entry and fold its aggregates into ``agg`` (token keys; hashed keys were
        counted per occurrence): (hash128, count, records, first, inrec, last, display spelling)."""
        count, records, first, inrec, last = _unpack(v, self.multi)
        if key[0] == 0x23:
            return _from_bytes(key[1:], "big"), count, records, first, inrec, last, self.spell.get(key)
        _k, tidx, info, canon = _analyze(key)
        spelling = self.spell.get(key, key)
        agg.add(tidx, info, count, first, inrec, spelling)
        return key_hash(_canon_of(_k, canon)), count, records, first, inrec, last, spelling

    def to_sketch(self) -> None:
        self.hll = hll = _HLL()
        self.mg = mg = _MG(self.set.mg_counters(extra=1))
        n = 0
        cancel = self.set.cancel
        for key, v in self.d.items():
            n += 1
            if not n & 0xFFFF:
                check(cancel)
            h, count, records, first, inrec, last, spelling = self._fold(key, v, self.agg)
            hll.add(h & _M64)
            mg.add_weighted(h, count, records, first, inrec, last,
                            spelling if spelling is not None and len(spelling) <= LONG_TOKEN else None)
        mg.prune()
        self.d = {}
        self.spell = {}
        self.nbytes = 0
        self.snap_hll = self.snap_mg = None
        self.mode = _SKETCH
        log.debug("stats: a field switched to sketches (HyperLogLog + Misra-Gries, %d counters)", mg.k)

    def _entries(self, open_record: int = -1, snapshot: bool = False) -> list[int]:
        """The aggregated run entries of the current dict (aggregates folded). The epochs of an
        accumulator of the call hold disjoint records -- except the record an epoch ended in the
        middle of (``open_record``; the next epoch begins inside it, ``self.split``): a value's
        entry counts every record but that one, and a *marker* entry (flagged, count 0) stands for
        it, so that the final walk counts it once whatever epochs hold the value (a multi-valued
        field only: a single-valued one holds one value per record). Those of a single-record
        accumulator all hold that record: their entries are flagged and carry no record count (the
        final walk counts the record once per value). ``snapshot``: the values also go to the live
        snapshot's summary (``snap_hll`` / ``snap_mg``)."""
        entries: list[int] = []
        n = 0
        cancel = self.set.cancel
        flag = 1 if self.set.single else 0
        split = self.split if self.multi else -1
        if not self.multi:
            open_record = -1
        snap_hll = self.snap_hll if snapshot else None
        snap_mg = self.snap_mg if snapshot else None
        append = entries.append
        for key, v in self.d.items():
            n += 1
            if not n & 0xFFFF:
                check(cancel)
            h, count, records, first, inrec, last, sp = self._fold(key, v, self.agg)
            h96 = h >> 32
            if flag:
                _spill_entries(entries, h96, first, inrec, count, 0, 1)
            else:
                marks: list[tuple[int, int]] = []
                if split >= 0 and first == split:
                    records -= 1
                    marks.append((split, inrec))
                if open_record >= 0 and last == open_record and not (marks and open_record == split):
                    records -= 1
                    marks.append((open_record, inrec if first == open_record else _M15))
                _spill_entries(entries, h96, first, inrec, count, records, 0)
                for rec, ir in marks:
                    append((h96 << 96) | (rec << 56) | (min(ir, _M15) << 41) | (1 << 40))
            if snap_hll is not None:
                snap_hll.add(h & _M64)
                snap_mg.add_weighted(h, count, records, first, inrec, last,   # type: ignore[union-attr]
                                     sp if sp is not None and len(sp) <= LONG_TOKEN else None)
        return entries

    def spill(self, open_record: int = -1) -> None:
        """Write the dict as a sorted run (exact mode; ``open_record``: see :meth:`_entries`)."""
        if not self.set.single and self.snap_hll is None:
            self.snap_hll = _HLL()
            self.snap_mg = _MG(max(SNAP_MG, 10 * self.set.top))
        entries = self._entries(open_record, snapshot=not self.set.single)
        if self.runs is None:
            self.runs = self.set.new_runs()
        self.runs.spill(entries)
        self.d = {}
        self.spell = {}
        self.nbytes = 0
        self.mode = _SPILL
        self.split = open_record if self.multi else -1

    # -- merging a per-record accumulator --------------------------------------------------
    def absorb(self, other: _FieldAcc) -> None:
        """Add the statistics of ``other`` -- an accumulator of the same field filled from ONE
        record that comes after every record counted here -- to this one."""
        if other.mode == _SPILL and other.d:
            # the record's last epoch too (its aggregates are folded now, before they are merged):
            # every entry of a spilled single-record accumulator is flagged -- its epochs share
            # the record -- so the walk counts the record once
            other.spill()
        self.agg.merge(other.agg)       # occurrences counted one by one, and other's folds
        self.uncounted += other.uncounted
        if other.mode == _SKETCH:
            if self.mode != _SKETCH:
                self.to_sketch()
            self.hll.merge(other.hll)   # type: ignore[union-attr]
            self.mg.merge(other.mg)     # type: ignore[union-attr]
            return
        if other.mode == _SPILL:
            if self.mode == _DICT:
                self.mode = _SPILL
            if self.runs is None:
                self.runs = self.set.new_runs()
            self.runs.paths.extend(other.runs.paths)    # (files removed with the call's registry)
            return
        if self.mode == _SKETCH:
            hll = self.hll
            mg = self.mg
            for key, v in other.d.items():
                h, count, records, first, inrec, last, spelling = other._fold(key, v, self.agg)
                hll.add(h & _M64)   # type: ignore[union-attr]
                mg.add_weighted(h, count, records, first, inrec, last,   # type: ignore[union-attr]
                                spelling if spelling is not None and len(spelling) <= LONG_TOKEN else None)
            return
        sp = other.spell
        for key, v in other.d.items():
            count, records, first, inrec, last = _unpack(v, other.multi)
            self.add_entry(key, count, records, first, inrec, last, sp.get(key))

    def discard(self) -> None:
        """Forget everything (a per-record accumulator whose record is read again)."""
        self.d = {}
        self.spell = {}
        self.nbytes = 0
        self.hll = self.mg = self.snap_hll = self.snap_mg = None
        if self in self.set.accs:
            self.set.accs.remove(self)

    # -- results ---------------------------------------------------------------------------
    def finish(self, top: int, progress: Progress | None) -> _Final:
        """Distinct values and the ``top`` most frequent ones (aggregates folded)."""
        if self.mode == _SKETCH:
            mg = self.mg
            assert mg is not None and self.hll is not None
            distinct = max(int(round(self.hll.estimate())), len(mg.c))
            best = heapq.nsmallest(top, mg.c.items(), key=_mg_order) if top else []
            tops = [_Top(e[0], e[1], e[2], e[3], e[5], h, 128, mg.d) for h, e in best]
            return _Final(distinct, False, HLL_RSE, tops, False, mg.d)
        if self.mode == _SPILL:
            return self._finish_spill(top, progress)
        return self._finish_dict(top)

    def _finish_dict(self, top: int) -> _Final:
        groups: dict[bytes, list[Any]] = {}
        get = groups.get
        agg = self.agg
        spell = self.spell
        multi = self.multi
        n = 0
        cancel = self.set.cancel
        for key, v in self.d.items():
            n += 1
            if not n & 0xFFFF:
                check(cancel)
            count, records, first, inrec, _last = _unpack(v, multi)
            if key[0] == 0x23:
                gkey = key
                spelling = spell.get(key)
            else:
                gkey, tidx, info, _canon = _analyze(key)
                spelling = spell.get(key, key)
                agg.add(tidx, info, count, first, inrec, spelling)
            g = get(gkey)
            if g is None:
                groups[gkey] = [count, records, first, inrec, spelling]
            else:
                g[0] += count
                g[1] += records
                if (first, inrec) < (g[2], g[3]):
                    g[2] = first
                    g[3] = inrec
                    g[4] = spelling
        best = heapq.nsmallest(top, groups.items(), key=_group_order) if top else []
        tops = []
        for gkey, g in best:
            if gkey[0] == 0x23:
                tops.append(_Top(g[0], g[1], g[2], g[3], g[4], _from_bytes(gkey[1:], "big"), 128, 0))
            else:
                tops.append(_Top(g[0], g[1], g[2], g[3], g[4] if g[4] is not None else gkey, None, 128, 0))
        return _Final(len(groups), True, None, tops, True, 0)

    def _finish_spill(self, top: int, progress: Progress | None) -> _Final:
        entries = self._entries()
        self.d = {}
        self.spell = {}
        runs = self.runs
        assert runs is not None
        total = len(entries)
        for p in runs.paths:
            try:
                total += p.stat().st_size // 24
            except OSError:  # pragma: no cover - our own run files
                pass
        if progress is not None:
            progress.set_total(max(1, total))
        heap: list[tuple[int, int, int, int, int]] = []
        distinct = 0
        cancel = self.set.cancel
        stream = runs.merged(entries)
        prev = -1
        count = records = first = inrec = 0
        flagged = -1          # the record of the last flagged entry of the group (counted once)
        done = 0
        try:
            while True:
                batch = list(itertools.islice(stream, 16384))
                if not batch:
                    break
                check(cancel)
                for e in batch:
                    h = e >> 96
                    if h != prev:
                        if prev >= 0:
                            distinct += 1
                            if top:
                                _offer_top(heap, top, (count, -first, -inrec, -prev, records))
                        prev = h
                        first = (e >> 56) & _M40
                        inrec = (e >> 41) & _M15
                        count = (e >> 20) & _M20
                        flagged = -1
                        if e >> 40 & 1:
                            records = 1
                            flagged = first
                        else:
                            records = e & _M20
                    else:
                        count += (e >> 20) & _M20
                        if e >> 40 & 1:
                            f = (e >> 56) & _M40
                            if f != flagged:
                                records += 1
                                flagged = f
                        else:
                            records += e & _M20
                done += len(batch)
                if progress is not None:
                    progress.update(done)
            if prev >= 0:
                distinct += 1
                if top:
                    _offer_top(heap, top, (count, -first, -inrec, -prev, records))
        finally:
            stream.close()
        tops = [_Top(c, r, -f, -i, None, -nh, 96, 0) for c, f, i, nh, r in sorted(heap, reverse=True)]
        return _Final(distinct, True, None, tops, True, 0)


def _mg_order(item: tuple[int, list[Any]]) -> tuple[int, int, int]:
    e = item[1]
    return -e[0], e[2], e[3]


def _group_order(item: tuple[bytes, list[Any]]) -> tuple[int, int, int]:
    g = item[1]
    return -g[0], g[2], g[3]


def _offer_top(heap: list[tuple[int, int, int, int, int]], top: int, item: tuple[int, int, int, int, int]) -> None:
    """Keep the ``top`` best groups (count, then the earliest first occurrence, then the hash)
    in a min-heap."""
    if len(heap) < top:
        heapq.heappush(heap, item)
    elif item > heap[0]:
        heapq.heapreplace(heap, item)


@dataclass
class _Top:
    """One top value before its display text is known: counts, the first occurrence (ordinal,
    index inside the record), a token of it (``token``; ``None``: read it again from the first
    record, matching ``h`` -- the top ``bits`` of the canonical hash), the Misra-Gries error."""

    count: int
    records: int
    first: int
    inrec: int
    token: bytes | None
    h: int | None
    bits: int
    error: int


@dataclass
class _Final:
    distinct: int
    distinct_exact: bool
    rse: float | None
    top: list[_Top]
    top_exact: bool
    bound: int


def _assign(pending: dict[tuple[int, int], list[_Top]], tok: bytes) -> None:
    """The top values waiting for a token with ``tok``'s canonical hash get ``tok``."""
    hh = _token_hash(tok)
    for key in ((128, hh), (96, hh >> 32)):
        tops = pending.pop(key, None)
        if tops:
            for tp in tops:
                tp.token = tok


class _Matched(Exception):
    """Internal: every wanted top value of a streamed record has its token."""


class _MatchProbe:
    """Probe (``stream.StreamProbe`` protocol) of :meth:`_Engine._match_streamed`: hashes each
    matched scalar once; containers are only counted."""

    __slots__ = ("pending", "cancel", "n", "containers")

    def __init__(self, pending: dict[tuple[int, int], list[_Top]], cancel: CancelToken | None) -> None:
        self.pending = pending
        self.cancel = cancel
        self.n = 0
        self.containers = 0

    def value(self, v: Any) -> None:
        self.n += 1
        if not self.n & 4095:
            check(self.cancel)
        _assign(self.pending, _dump(v))
        if not self.pending:
            raise _Matched()

    def open(self, is_map: bool) -> bool:
        self.containers += 1
        return False

    def child(self, kind: str, v: Any) -> None:
        pass

    def close(self) -> None:
        pass


# ----------------------------------------------------------------------------------
# the fields
# ----------------------------------------------------------------------------------
class _Field:
    """A requested field: its pattern, strategy, presence context and extraction slots."""

    __slots__ = ("index", "text", "field", "path", "pattern", "is_key", "multi", "ctx", "cpat", "rest", "strategy",
                 "pi", "ci", "cci", "single_extractor")

    def __init__(self, index: int, text: str, pattern: PathPattern | None) -> None:
        self.index = index
        self.text = text
        self.pattern = pattern
        self.is_key = pattern is None
        self.pi = -1
        self.ci = -1
        self.cci = -1                 # elem1 fields: the context path in the count-only extractor
        self.single_extractor: Any = None
        self.cpat: PathPattern | None = None
        self.rest: PathPattern | None = None
        if pattern is None:
            self.field = KEY_FIELD
            self.path: tuple = (KEY_FIELD,)
            self.multi = False
            self.ctx = "record"
            self.strategy = "key"
            return
        segs = pattern.segments
        self.field = render_path(pattern)
        self.path = tuple(segs)
        self.multi = any(isinstance(s, Wild) for s in segs)
        self.strategy = "parse" if any(s is Wild.DESCENDANTS for s in segs) else "extract"
        self.ctx = "record"
        if self.strategy == "extract":
            for i in range(len(segs) - 2, -1, -1):
                if segs[i] is Wild.ANY_INDEX:
                    rest = segs[i + 1:]
                    self.cpat = PathPattern(segs[:i + 1])
                    self.rest = PathPattern(rest)
                    self.ctx = "elem1" if not any(isinstance(s, Wild) for s in rest) else "elem"
                    break

    @property
    def context_text(self) -> str:
        return "$" if self.cpat is None else render_path(self.cpat)


def _parse_fields(fields: Any) -> tuple[list[_Field], list[int]]:
    """The distinct fields to compute and, for every requested field, its index among them."""
    if isinstance(fields, str):
        fields = [fields]
    try:
        items = list(fields)
    except TypeError:
        raise UserInputError("stats.error.fields", value=repr(fields)) from None
    if not items:
        raise UserInputError("stats.error.no_fields")
    out: list[_Field] = []
    by_key: dict[Any, int] = {}
    order: list[int] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise UserInputError("stats.error.field", value=repr(item))
        if item.strip() == KEY_FIELD:
            pattern = None
            ident: Any = KEY_FIELD
        else:
            pattern = parse_pattern(item)    # PathSyntaxError (localized) for a bad path
            ident = pattern.segments
        idx = by_key.get(ident)
        if idx is None:
            idx = by_key[ident] = len(out)
            out.append(_Field(idx, item, pattern))
        order.append(idx)
    return out, order


def _check_top(top: Any) -> int:
    if isinstance(top, bool) or not isinstance(top, int) or top < 0 or top > TOP_MAX:
        raise UserInputError("stats.error.top", value=repr(top), max=TOP_MAX)
    return int(top)


def _check_sample(sample: Any) -> int | None:
    if sample is None:
        return None
    if isinstance(sample, bool) or not isinstance(sample, int) or sample < 1:
        raise UserInputError("stats.error.sample", value=repr(sample))
    return int(sample)


def _check_budget(memory_limit: Any, settings: Any) -> int:
    if memory_limit is None:
        return max(1, int(settings.memory_limit_bytes) // 8)
    if isinstance(memory_limit, bool) or not isinstance(memory_limit, int) or memory_limit < 1:
        raise UserInputError("stats.error.memory_limit", value=repr(memory_limit))
    return int(memory_limit)


def _check_strategy(strategy: Any) -> str:
    if strategy not in ("auto", "extract", "parse"):
        raise ValueError(f"unknown strategy {strategy!r} (auto, extract or parse)")
    if strategy == "auto":
        return "extract" if core.BACKEND == "rust" else "parse"
    return strategy


def _is_sequence(src: Any) -> bool:
    return (hasattr(src, "__len__") and hasattr(src, "__getitem__")
            and not isinstance(src, (dict, str, bytes, bytearray)))


class _Scope:
    """The records examined, in scope order: ``source`` for ``iter_blocks`` and the position of
    each ordinal (the index of a record in that order)."""

    def __init__(self, doc: Any, src: Any, sample: int | None, cancel: CancelToken | None) -> None:
        count = int(doc.record_count)
        self.seq: Any = None
        self.source: Any = None
        self.unique = True
        if src is None:
            self.n = count
            if sample is not None and count > sample:
                self.seq = self.source = array("Q", sample_positions(count, sample))
                self.n = len(self.seq)
            return
        if isinstance(src, RecordSet):
            self.unique = src.is_ascending()
        elif isinstance(src, range):
            self.unique = src.step > 0
        else:
            self.unique = False
        seq = src if _is_sequence(src) else _materialize(src, cancel)
        n = len(seq)
        if sample is not None and n > sample:
            seq = array("Q", (int(seq[p]) for p in sample_positions(n, sample)))
            n = len(seq)
        self.seq = self.source = seq
        self.n = n

    def position(self, o: int) -> int:
        return o if self.seq is None else int(self.seq[o])


def _materialize(src: Iterable[int], cancel: CancelToken | None) -> array:
    out = array("Q")
    n = 0
    for pos in src:
        out.append(int(pos))
        n += 1
        if not n & 0xFFFF:
            check(cancel)
    return out


# ----------------------------------------------------------------------------------
# streamed records (huge records and the fallbacks)
# ----------------------------------------------------------------------------------
class _RecState:
    """One field's statistics of ONE streamed record, merged into the field when the record
    succeeds. Also the probe of the field's pattern (the ``StreamProbe`` protocol)."""

    __slots__ = ("f", "acc", "o", "n", "base", "present", "containers", "lens", "ctx")

    def __init__(self, f: _Field, accset: _AccSet, o: int) -> None:
        self.f = f
        self.acc = _FieldAcc(f.multi, accset, f"{f.field}#{o}")
        self.o = o
        self.n = 0                    # values (index of the next one)
        self.base = 0                 # contexts (element contexts)
        self.present = 0
        self.containers = 0
        self.lens: list[list[Any]] = []
        self.ctx: list[bool] = []

    # -- the probe of the field's pattern --
    def value(self, v: Any) -> None:
        self.acc.add(_dump(v), self.o, self.n)
        self.n += 1
        if self.ctx:
            self.ctx[-1] = True
        self.acc.set.tick()

    def open(self, is_map: bool) -> bool:
        self.n += 1
        self.containers += 1
        self.acc.agg.types[T_OBJECT if is_map else T_ARRAY] += 1
        self.lens.append([is_map, 0])
        if self.ctx:
            self.ctx[-1] = True
        return True

    def child(self, kind: str, v: Any) -> None:
        self.lens[-1][1] += 1

    def close(self) -> None:
        is_map, n = self.lens.pop()
        self.acc.agg.add_len(is_map, n)

    def refill(self, values: list[Any]) -> None:
        """Every value again (the containers' canonical forms), from ``stream_values``."""
        accset = self.acc.set
        self.acc.discard()
        self.acc = acc = _FieldAcc(self.f.multi, accset, f"{self.f.field}#{self.o}")
        tick = accset.tick
        for i, v in enumerate(values):
            acc.add(_dump(v), self.o, i)
            tick()
        self.n = len(values)
        self.containers = 0


class _CtxProbe:
    """Probe of a field's context pattern ``C``: counts the contexts and, for ``elem`` fields,
    the contexts holding a value (the field's values arrive while their context is open)."""

    __slots__ = ("st", "track")

    def __init__(self, st: _RecState, track: bool) -> None:
        self.st = st
        self.track = track

    def value(self, v: Any) -> None:
        self.st.base += 1

    def open(self, is_map: bool) -> bool:
        self.st.base += 1
        if self.track:
            self.st.ctx.append(False)
            return True
        return False

    def child(self, kind: str, v: Any) -> None:
        pass

    def close(self) -> None:
        if self.st.ctx.pop():
            self.st.present += 1


# ----------------------------------------------------------------------------------
# presence counters
# ----------------------------------------------------------------------------------
class _Presence:
    __slots__ = ("scanned", "base", "present", "values", "too_large", "first_too_large", "unreadable",
                 "first_unreadable", "first_uncounted")

    def __init__(self) -> None:
        self.scanned = 0
        self.base = 0
        self.present = 0
        self.values = 0
        self.too_large = 0
        self.first_too_large: int | None = None
        self.unreadable = 0
        self.first_unreadable: int | None = None
        self.first_uncounted: int | None = None


# ----------------------------------------------------------------------------------
# the engine
# ----------------------------------------------------------------------------------
class _Engine:
    """One pass over the scope (module docstring, "Algorithm")."""

    def __init__(self, doc: Any, fields: list[_Field], scope: _Scope, *, budget: int, exact: bool, top: int,
                 settings: Any, strategy: str, cancel: CancelToken | None,
                 on_partial: Callable[[list[FieldStats]], Any] | None, order: list[int], started: float) -> None:
        self.doc = doc
        self.member = bool(doc.member_records)
        self.fields = fields
        self.scope = scope
        self.top = top
        self.exact = exact
        self.cancel = cancel
        self.on_partial = on_partial
        self.order = order
        self.started = started
        self.budget = budget
        self.capture = capture_budget(settings)
        self.directory = work_dir(settings.cache_dir)
        self.registry: list[Any] = []
        self.accset = _AccSet(budget, exact, self.directory, cancel, self.registry, top=top,
                              hist_k=_hist_k(budget, len(fields)))
        self.accs = [_FieldAcc(f.multi, self.accset, f.field) for f in fields]
        self.pres = [_Presence() for _ in fields]
        self.seen = None if scope.unique else bytearray((int(doc.record_count) + 7) >> 3)
        # strategies: the pure-Python core extracts slower than it parses ("auto")
        for f in fields:
            if f.strategy == "extract" and strategy == "parse":
                f.strategy = "parse"
        self.key_fields = [f for f in fields if f.strategy == "key"]
        self.parse_fields = [f for f in fields if f.strategy == "parse"]
        self.extract_fields = [f for f in fields if f.strategy == "extract"]
        self.stream_fields = self.extract_fields + self.parse_fields
        # a record that may hold more values than this is streamed for multi-valued extract fields
        # (the extractor's output costs 32 bytes per value, twice while it is made: review R5A-1)
        self.multi_extract = any(f.multi or f.cpat is not None for f in self.extract_fields)
        self.stream_values = max(4096, int(settings.memory_limit_bytes) // EXTRACT_VALUE_SHARE)
        paths: list[list[tuple]] = []
        slots: dict[tuple, int] = {}

        def slot(p: PathPattern) -> int:
            i = slots.get(p.segments)
            if i is None:
                i = slots[p.segments] = len(paths)
                paths.append(p.to_core())
            return i

        # the contexts of elem1 fields are only counted: their own extractor, counted per path
        # with array.count (no Python loop over their quads)
        cpaths: list[list[tuple]] = []
        cslots: dict[tuple, int] = {}
        for f in self.extract_fields:
            f.pi = slot(f.pattern)   # type: ignore[arg-type]
            if f.cpat is not None:
                if f.ctx == "elem":
                    f.ci = slot(f.cpat)
                else:
                    i = cslots.get(f.cpat.segments)
                    if i is None:
                        i = cslots[f.cpat.segments] = len(cpaths)
                        cpaths.append(f.cpat.to_core())
                    f.cci = i
        self.npaths = len(paths)
        cls = _EXTRACTOR if _EXTRACTOR is not None else core.PathExtractor
        self.extractor_cls = cls
        self.extractor = cls(paths, member_spans=self.member) if paths else None
        self.counter = cls(cpaths, member_spans=self.member) if cpaths else None
        self.next_partial = time.monotonic() + PARTIAL_SECONDS

    # -- the pass ----------------------------------------------------------------------------
    def scan(self, progress: Progress | None) -> None:
        doc = self.doc
        blocks = doc.iter_blocks(self.scope.source, progress=progress, cancel=self.cancel)
        o = 0
        try:
            for blk in blocks:
                check(self.cancel)
                n = len(blk.indices)
                rep = self._repeated(blk.indices)
                if blk.huge:
                    if not rep:
                        self._huge(int(blk.indices[0]), blk.span(0), o)
                        self._maybe_partial()
                else:
                    big = self._big_records(blk)
                    for lo in range(0, n, CHUNK_RECORDS):
                        check(self.cancel)
                        hi = min(n, lo + CHUNK_RECORDS)
                        a = lo
                        for k in [b for b in big if lo <= b < hi] + [hi]:
                            if a < k:
                                reps = None
                                if rep:
                                    reps = {r - a for r in rep if a <= r < k} or None
                                self._chunk(blk, a, k, o + a, reps)
                            if k < hi:
                                if not (rep and k in rep):
                                    self._big_record(blk, k, o + k)
                                a = k + 1
                        self.accset.check_budget()
                        self._maybe_partial()
                o += n
                self.accset.check_budget()
        finally:
            blocks.close()

    def _big_records(self, blk: Any) -> list[int]:
        """Block offsets of the records to stream instead of extracting: with a multi-valued
        extract field, a record that may hold more than ``stream_values`` values (its commas: an
        upper bound of what the extractor returns, 32 bytes each -- review R5A-1)."""
        if not self.multi_extract or len(blk.data) <= self.stream_values:
            return []
        sp = _u64(blk.spans)
        data = blk.data
        limit = self.stream_values
        out = []
        for k in range(len(blk.indices)):
            s = sp[2 * k]
            e = sp[2 * k + 1]
            if e - s > limit and data.count(b",", s, e) > limit:
                out.append(k)
        return out

    def _big_record(self, blk: Any, k: int, o: int) -> None:
        """One record of :meth:`_big_records`: streamed for the extract fields (bounded memory,
        the ijson deviations of streamed records), the other fields as usual."""
        pos = int(blk.indices[k])
        log.debug("stats: record #%d may hold millions of values: streamed", pos)
        if self.extract_fields:
            self._stream_record(pos, self.doc.record_span(pos), o, self.extract_fields)
        if self.parse_fields:
            self._parse_chunk(blk, k, k + 1, o, None, self.parse_fields)
        if self.key_fields:
            self._key_chunk(blk, k, k + 1, o, None, 1)

    def _repeated(self, idx: array) -> set[int] | None:
        """Block offsets of positions examined before (sources that may repeat positions)."""
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

    def _chunk(self, blk: Any, lo: int, hi: int, o_lo: int, reps: set[int] | None) -> None:
        data = blk.data
        live = hi - lo - (len(reps) if reps else 0)
        if self.extractor is not None:
            try:
                q = _quads(self.extractor.extract(data, blk.spans[16 * lo:16 * hi]))
                counts = self._context_counts(data, blk.spans[16 * lo:16 * hi], reps)
            except ValueError:
                self._chunk_records(blk, lo, hi, o_lo, reps)
                return
            buckets = self._buckets(q, reps)
            for f in self.extract_fields:
                self._extract_field(f, q, buckets, data, o_lo, live, counts)
        if self.parse_fields:
            self._parse_chunk(blk, lo, hi, o_lo, reps, self.parse_fields)
        if self.key_fields:
            self._key_chunk(blk, lo, hi, o_lo, reps, live)

    def _chunk_records(self, blk: Any, lo: int, hi: int, o_lo: int, reps: set[int] | None) -> None:
        """A chunk the extractor refused (some record it cannot read): record by record; a
        record it refuses is parsed, else streamed."""
        data = blk.data
        spans = blk.spans
        for k in range(lo, hi):
            if reps and k - lo in reps:
                continue
            o = o_lo + k - lo
            try:
                q = _u64(self.extractor.extract(data, spans[16 * k:16 * k + 16]))   # type: ignore[union-attr]
            except ValueError as exc:
                log.debug("stats: extraction failed for record #%d (%s); parsing it", int(blk.indices[k]), exc)
                self._parse_chunk(blk, k, k + 1, o, None, self.extract_fields)
            else:
                # re-based: the quads' record index is 0 for this one record
                buckets = self._buckets(q, None)
                try:
                    counts = self._context_counts(data, spans[16 * k:16 * k + 16], None)
                except ValueError:   # pragma: no cover - the main extractor read the record
                    counts = None
                for f in self.extract_fields:
                    self._extract_field(f, q, buckets, data, o, 1, counts)
            if self.parse_fields:
                self._parse_chunk(blk, k, k + 1, o, None, self.parse_fields)
            if self.key_fields:
                self._key_chunk(blk, k, k + 1, o, None, 1)

    def _context_counts(self, data: bytes, spans: bytes, reps: set[int] | None) -> array | None:
        """The path index of every context instance of the elem1 fields in these records
        (``array.count(i)`` = the instances of context path ``i``)."""
        if self.counter is None:
            return None
        qc = _u64(self.counter.extract(data, spans))
        if reps:
            return array("Q", [qc[j + 1] for j in range(0, len(qc), 4) if qc[j] not in reps])
        return qc[1::4]

    def _buckets(self, q: Any, reps: set[int] | None) -> list[Any]:
        """The quad offsets of every path (``array('Q')``: 8 bytes per value -- lists of ints
        took ~40, review R5A-1)."""
        n = len(q)
        if self.npaths == 1 and not reps:
            return [range(0, n, 4)]
        out: list[Any] = [array("Q") for _ in range(self.npaths)]
        if reps:
            for j in range(0, n, 4):
                if q[j] not in reps:
                    out[q[j + 1]].append(j)
        else:
            for j in range(0, n, 4):
                out[q[j + 1]].append(j)
        return out

    # -- extracted fields --------------------------------------------------------------------
    def _extract_field(self, f: _Field, q: array, buckets: list[Any], data: bytes, o_lo: int, live: int,
                       counts: array | None) -> None:
        js = buckets[f.pi]
        p = self.pres[f.index]
        nv = len(js)
        p.values += nv
        p.scanned += live
        if f.ctx == "record":
            p.base += live
            if not f.multi:
                p.present += nv
            elif isinstance(js, range):
                p.present += len(set(q[0::4]))
            else:
                p.present += len({q[j] for j in js})
        elif f.ctx == "elem1":
            p.base += counts.count(f.cci)   # type: ignore[union-attr]
            p.present += nv
        else:
            cj = buckets[f.ci]
            p.base += len(cj)
            if nv:
                p.present += _contexts_holding(q, cj, js)
        if nv:
            self._feed(f, self.accs[f.index], q, js, data, o_lo)

    def _feed(self, f: _Field, acc: _FieldAcc, q: Any, js: Any, data: bytes, o_lo: int) -> None:
        """The values ``js`` (quad offsets) of one chunk into ``acc``, in slices of
        :data:`FEED_SLICE` values with a budget check between them: one record can hold millions
        of values (review R5A-1). A field that overflows in the middle of a record goes on in its
        new mode; a multi-valued field spilled there marks that record (``_FieldAcc._entries``)."""
        n = len(js)
        state = [-1, 0, 0]      # multi-valued: the current record (block index), its value index, ordinal
        accset = self.accset
        start = 0
        while start < n:
            stop = min(n, start + FEED_SLICE)
            part = js[start:stop] if (start or stop < n) else js
            if acc.mode == _SKETCH:
                self._feed_sketch(f.multi, acc, q, part, data, o_lo, state)
            else:
                # (not ``acc.nbytes += ...``: the slow paths add to acc.nbytes during the call)
                if f.multi:
                    nb = self._feed_multi(acc, q, part, data, o_lo, state)
                else:
                    nb = self._feed_single(acc, q, part, data, o_lo)
                acc.nbytes += nb
            start = stop
            if start < n and accset.over():
                # the next value in the same record: the record is open
                accset.check_budget(acc, state[2] if f.multi and q[js[start]] == state[0] else -1)

    @staticmethod
    def _feed_sketch(multi: bool, acc: _FieldAcc, q: Any, js: Any, data: bytes, o_lo: int, state: list[int]) -> None:
        add = acc.sketch_add
        if multi:
            cur, inrec, o = state
            for j in js:
                k = q[j]
                if k != cur:
                    cur = k
                    inrec = 0
                    o = o_lo + k
                else:
                    inrec += 1
                add(data[q[j + 2]:q[j + 3]], o, inrec)
            state[:] = [cur, inrec, o]
        else:
            for j in js:
                add(data[q[j + 2]:q[j + 3]], o_lo + q[j], 0)

    @staticmethod
    def _feed_single(acc: _FieldAcc, q: Any, js: Any, data: bytes, o_lo: int) -> int:
        """The hot loop of a single-valued field; returns the bytes the new entries cost."""
        d = acc.d
        get = d.get
        cost = acc.cost
        nb = 0
        slow = acc.add_single
        for j in js:
            s = q[j + 2]
            e = q[j + 3]
            if e - s > LONG_TOKEN:
                slow(data[s:e], o_lo + q[j])
                continue
            tok = data[s:e]
            v = get(tok)
            if v is None:
                d[tok] = ((o_lo + q[j]) << 40) | 1
                nb += e - s + cost
            else:
                d[tok] = v + 1
        return nb

    @staticmethod
    def _feed_multi(acc: _FieldAcc, q: Any, js: Any, data: bytes, o_lo: int, state: list[int]) -> int:
        """The hot loop of a multi-valued field; returns the bytes the new entries cost."""
        d = acc.d
        get = d.get
        cost = acc.cost
        nb = 0
        slow = acc.add_multi
        quick = _quick_canonical
        cur, inrec, o = state
        for j in js:
            k = q[j]
            if k != cur:
                cur = k
                inrec = 0
                o = o_lo + k
            else:
                inrec += 1
            s = q[j + 2]
            e = q[j + 3]
            tok = data[s:e]
            if e - s > LONG_TOKEN or not (0x5C not in tok if tok[0] == 0x22 else quick(tok)):
                slow(tok, o, inrec)
                continue
            v = get(tok)
            if v is None:
                d[tok] = (o << 144) | ((inrec if inrec < _M24 else _M24) << 120) | (o << 80) | _REC_INC
                nb += e - s + cost
            else:
                last = (v >> 80) & _M40
                if last == o:
                    d[tok] = v + 1
                else:
                    d[tok] = v + ((o - last) << 80) + _REC_INC
        state[:] = [cur, inrec, o]
        return nb

    # -- parsed fields -------------------------------------------------------------------------
    def _parse_chunk(self, blk: Any, lo: int, hi: int, o_lo: int, reps: set[int] | None,
                     fields: list[_Field]) -> None:
        data = blk.data
        sp = _u64(blk.spans[16 * lo:16 * hi])
        member = self.member
        loads = codec.loads
        accset_over = self.accset.over
        fed = 0
        for k in range(hi - lo):
            if reps and k in reps:
                continue
            o = o_lo + k
            s = sp[2 * k]
            e = sp[2 * k + 1]
            raw = data[s:e]
            value: Any = MISSING
            if e - s <= PARSE_MAX_BYTES:
                try:
                    if member:
                        raw = raw[_split_member(raw)[1]:]
                    value = loads(raw)
                except _DECODE_ERRORS:
                    value = MISSING
            if value is MISSING:
                pos = int(blk.indices[lo + k])
                self._stream_record(pos, self.doc.record_span(pos), o, fields)
                continue
            for f in fields:
                fed += self._add_parsed(f, value, o)
            if fed >= FEED_SLICE:        # between records, every FEED_SLICE values (review R5A-1)
                fed = 0
                if accset_over():
                    self.accset.check_budget()

    def _add_parsed(self, f: _Field, value: Any, o: int) -> int:
        """The field's values in the parsed record ``value`` (returns how many)."""
        p = self.pres[f.index]
        p.scanned += 1
        if f.cpat is None:
            vals = f.pattern.values(value)   # type: ignore[union-attr]
            p.base += 1
            if vals:
                p.present += 1
        else:
            insts = f.cpat.values(value)
            p.base += len(insts)
            vals = []
            rest = f.rest
            for inst in insts:
                rv = rest.values(inst)   # type: ignore[union-attr]
                if rv:
                    p.present += 1
                    vals.extend(rv)
        p.values += len(vals)
        acc = self.accs[f.index]
        dumps = _dump
        for i, v in enumerate(vals):
            acc.add(dumps(v), o, i)
        return len(vals)

    # -- the member key ------------------------------------------------------------------------
    def _key_chunk(self, blk: Any, lo: int, hi: int, o_lo: int, reps: set[int] | None, live: int) -> None:
        for f in self.key_fields:
            p = self.pres[f.index]
            p.scanned += live
            p.base += live
        if not self.member:
            return
        data = blk.data
        sp = _u64(blk.spans[16 * lo:16 * hi])
        for k in range(hi - lo):
            if reps and k in reps:
                continue
            try:
                key = _split_member(data[sp[2 * k]:sp[2 * k + 1]])[0]
            except ValueError as exc:
                self._unreadable(self.key_fields, int(blk.indices[lo + k]), exc, counted=True)
                continue
            self._add_key(key, o_lo + k)

    def _add_key(self, key: str, o: int) -> None:
        tok = _dump(key)
        for f in self.key_fields:
            p = self.pres[f.index]
            p.present += 1
            p.values += 1
            self.accs[f.index].add(tok, o, 0)

    # -- streamed records ----------------------------------------------------------------------
    def _huge(self, pos: int, span: tuple[int, int], o: int) -> None:
        if self.key_fields:
            for f in self.key_fields:
                p = self.pres[f.index]
                p.scanned += 1
                p.base += 1
            if self.member:
                try:
                    key = self.doc.record_key(pos)
                except ValueError as exc:
                    self._unreadable(self.key_fields, pos, exc, counted=True)
                else:
                    if key is not None:
                        self._add_key(key, o)
        if self.stream_fields:
            self._stream_record(pos, span, o, self.stream_fields)

    def _stream_record(self, pos: int, span: tuple[int, int], o: int, fields: list[_Field]) -> None:
        """The fields' values of one record read with ijson (module docstring, "Huge records")."""
        # the record's accumulators get what the call's dicts leave of the budget (at least an eighth)
        budget = max(self.budget // 8, self.budget - self.accset.dict_bytes())
        accset = _AccSet(budget, self.exact, self.directory, self.cancel, self.registry, single=True, top=self.top,
                         hist_k=self.accset.hist_k)
        states = [_RecState(f, accset, o) for f in fields]
        patterns: list[PathPattern] = []
        probes: dict[int, list[Any]] = {}
        slots: dict[tuple, int] = {}

        def slot(p: PathPattern) -> int:
            i = slots.get(p.segments)
            if i is None:
                i = slots[p.segments] = len(patterns)
                patterns.append(p)
            return i

        for st in states:
            f = st.f
            if f.cpat is not None:     # the context first: it opens before the values inside it
                probes.setdefault(slot(f.cpat), []).append(_CtxProbe(st, f.ctx == "elem"))
            probes.setdefault(slot(f.pattern), []).append(st)   # type: ignore[arg-type]
        try:
            stream_probe(self.doc, span, patterns, probes, self.member, cancel=self.cancel)
        except DuplicateKeys:
            for st in states:
                st.acc.discard()
            log.debug("stats: duplicate keys in the streamed record #%d; reading it field by field", pos)
            for f in fields:
                self._stream_values(f, pos, span, o, accset)
            return
        except Cancelled:
            raise
        except JsonReshapeError as exc:
            if exc.key != "stream.error.parse":
                raise          # file changed, damaged edit layer, ...: not a property of the record
            self._unreadable(fields, pos, exc)
            return
        for st in states:
            if st.containers:
                try:
                    values = stream_values(self.doc, span, [st.f.pattern], self.member, max_bytes=self.capture,
                                           cancel=self.cancel)[0]
                except CaptureLimitExceeded:
                    st.acc.uncounted += st.containers
                    pres = self.pres[st.f.index]
                    if pres.first_uncounted is None:
                        pres.first_uncounted = pos
                else:
                    st.refill(values)
            self._commit(st)

    def _stream_values(self, f: _Field, pos: int, span: tuple[int, int], o: int, accset: _AccSet) -> None:
        """One field of a record with duplicate keys: its values with dict semantics."""
        st = _RecState(f, accset, o)
        try:
            if f.cpat is None:
                vals = stream_values(self.doc, span, [f.pattern], self.member, max_bytes=self.capture,
                                     cancel=self.cancel)[0]
                st.present = 1 if vals else 0
            else:
                insts = stream_values(self.doc, span, [f.cpat], self.member, max_bytes=self.capture,
                                      cancel=self.cancel)[0]
                st.base = len(insts)
                vals = []
                for inst in insts:
                    rv = f.rest.values(inst)   # type: ignore[union-attr]
                    if rv:
                        st.present += 1
                        vals.extend(rv)
        except CaptureLimitExceeded:
            st.acc.discard()
            p = self.pres[f.index]
            p.too_large += 1
            if p.first_too_large is None:
                p.first_too_large = pos
            return
        except Cancelled:
            raise
        except JsonReshapeError as exc:
            if exc.key != "stream.error.parse":
                raise
            st.acc.discard()
            self._unreadable([f], pos, exc)
            return
        st.refill(vals)
        self._commit(st, counted_present=True)

    def _commit(self, st: _RecState, counted_present: bool = False) -> None:
        f = st.f
        p = self.pres[f.index]
        p.scanned += 1
        p.values += st.n
        if f.ctx == "record":
            p.base += 1
            if st.n:
                p.present += 1
        elif f.ctx == "elem1" and not counted_present:
            p.base += st.base
            p.present += st.n
        else:
            p.base += st.base
            p.present += st.present
        acc = st.acc
        self.accs[f.index].absorb(acc)
        acc.discard()
        self.accset.check_budget()

    def _unreadable(self, fields: list[_Field], pos: int, exc: BaseException, counted: bool = False) -> None:
        """A record whose values cannot be read for ``fields`` (``counted``: already counted as
        scanned -- taken back)."""
        for f in fields:
            p = self.pres[f.index]
            if counted:
                p.scanned -= 1
                p.base -= 1
            p.unreadable += 1
            if p.first_unreadable is None:
                p.first_unreadable = pos
        log.warning("stats: the values of record #%d cannot be read: %s", pos, exc)

    # -- live snapshots ------------------------------------------------------------------------
    def _maybe_partial(self) -> None:
        if self.on_partial is None:
            return
        now = time.monotonic()
        if now < self.next_partial:
            return
        snap = [self._snapshot(f) for f in self.fields]
        cost = time.monotonic() - now
        self.next_partial = time.monotonic() + max(PARTIAL_SECONDS, PARTIAL_COST_FACTOR * cost)
        try:
            self.on_partial([snap[i] for i in self.order])
        except Cancelled:
            raise
        except Exception:  # never let a UI callback kill the computation
            log.exception("stats: on_partial callback failed")

    def _snapshot(self, f: _Field) -> FieldStats:
        acc = self.accs[f.index]
        p = self.pres[f.index]
        types = list(acc.agg.types)
        tops: list[ValueCount] = []
        hist = acc.agg.hist
        lens = acc.agg.lens
        if acc.mode == _SKETCH:
            assert acc.mg is not None and acc.hll is not None
            distinct = max(int(round(acc.hll.estimate())), len(acc.mg.c))
            for h, e in heapq.nsmallest(self.top, acc.mg.c.items(), key=_mg_order) if self.top else []:
                value, text = _decode_token(e[5])
                tops.append(ValueCount(value, text, e[0], e[1], self.scope.position(e[2]), acc.mg.d))
            bound = acc.mg.d
        else:
            multi = acc.multi
            if acc.d:
                # the numbers and string lengths of the dict are folded only at the end of the pass:
                # the live histogram gets them from a copy of the material (module docstring)
                hist = hist.copy()
                lens = lens.copy()
            for key, v in acc.d.items():
                if key[0] != 0x23:
                    c = v & _M40
                    ti = _type_of(key)
                    types[ti] += c
                    if ti == T_NUMBER:
                        try:
                            info = _number_info(key)
                        except ValueError:   # pragma: no cover - keys are validated tokens
                            continue
                        hist.add(info[0], c, info[1])
                    elif ti == T_STRING:
                        lens.add(_key_str_len(key), c)
            if multi:
                distinct = len(acc.d)       # canonical keys already
            else:
                # a single-valued field keys the raw spelling: count the values (1 and 1.0 are one),
                # so that a snapshot never shows more than the final count (review R5A-7)
                values: set[bytes] = set()
                quick = _quick_canonical
                for key in acc.d:
                    values.add(key if key[0] == 0x23 or quick(key) else _analyze(key)[0])
                distinct = len(values)
            items = heapq.nsmallest(self.top, acc.d.items(), key=lambda kv: (-(kv[1] & _M40),
                                                                             _unpack(kv[1], multi)[2]))
            bound = 0
            if acc.snap_hll is not None and acc.snap_mg is not None:
                # spilled epochs (review R5A-7: the snapshots showed the current epoch only, distinct
                # 0 and no top values after every spill): their summary plus the current epoch; the
                # count never goes below an earlier snapshot's (the estimate has an error)
                distinct = max(distinct, int(round(acc.snap_hll.estimate())), acc.snap_last)
                rows = {h: [e[0], e[1], e[2], e[3], e[5]] for h, e in acc.snap_mg.c.items()}
                for key, v in items:
                    count, records, first, inrec, _last = _unpack(v, multi)
                    k2, _tidx, _info, canon = _analyze(key) if key[0] != 0x23 else (key, 0, None, None)
                    h = _from_bytes(k2[1:], "big") if k2[0] == 0x23 else key_hash(_canon_of(k2, canon))
                    token = acc.spell.get(key, None if key[0] == 0x23 else key)
                    row = rows.get(h)
                    if row is None:
                        rows[h] = [count, records, first, inrec, token]
                    else:
                        row[0] += count
                        row[1] += records
                        if (first, inrec) < (row[2], row[3]):
                            row[2], row[3], row[4] = first, inrec, token if token is not None else row[4]
                best = heapq.nsmallest(self.top, rows.values(), key=lambda r: (-r[0], r[2], r[3])) if self.top else []
                for count, records, first, _inrec, token in best:
                    value, text = _decode_token(token)
                    tops.append(ValueCount(value, text, count, records, self.scope.position(first), acc.snap_mg.d))
                bound = acc.snap_mg.d
            else:
                for key, v in items if self.top else []:
                    count, records, first, _inrec, _last = _unpack(v, multi)
                    token = acc.spell.get(key, None if key[0] == 0x23 else key)
                    value, text = _decode_token(token)
                    tops.append(ValueCount(value, text, count, records, self.scope.position(first)))
        ordered = {TYPE_NAMES[i]: types[i] for i in sorted(range(6), key=lambda i: (-types[i], i)) if types[i]}
        if acc.mode != _DICT:
            distinct = max(distinct, acc.snap_last)   # estimates: never below an earlier snapshot
        acc.snap_last = distinct
        return FieldStats(
            field=f.field, path=f.path, is_key=f.is_key, scanned=p.scanned, skipped=p.too_large + p.unreadable,
            base=p.base, present=p.present, missing=max(0, p.base - p.present), values=p.values,
            nulls=types[T_NULL], types=ordered, distinct=distinct, distinct_exact=False,
            distinct_rse=HLL_RSE if (acc.mode == _SKETCH or acc.snap_hll is not None) else None, top=tops,
            top_exact=False, top_bound=bound,
            number=None, string=None, arr_len=None, obj_keys=None, gen=getattr(self.doc, "gen", None),
            elapsed=time.perf_counter() - self.started, uncounted=acc.uncounted, multi=f.multi,
            context=f.context_text, partial=True, number_hist=hist if hist.n else None,
            length_hist=lens if lens.n else None)

    # -- results -------------------------------------------------------------------------------
    def finish(self, progress: Progress | None) -> list[FieldStats]:
        finals: list[_Final] = []
        spilled = [a for a in self.accs if a.mode == _SPILL]
        for i, (f, acc) in enumerate(zip(self.fields, self.accs)):
            check(self.cancel)
            sub = None
            if progress is not None and acc.mode == _SPILL and spilled:
                k = spilled.index(acc)
                sub = progress.child(k / len(spilled), (k + 1) / len(spilled))
            finals.append(acc.finish(self.top, sub))
            if sub is not None:
                sub.finish()
        check(self.cancel)
        self._resolve(finals)
        check(self.cancel)
        out: list[FieldStats] = []
        elapsed = time.perf_counter() - self.started
        for f, acc, fin in zip(self.fields, self.accs, finals):
            out.append(self._result(f, acc, fin, elapsed))
        return out

    def _resolve(self, finals: list[_Final]) -> None:
        """Read again the first records of the top values without a kept token: the tokens of a
        field in a record are read once and hashed once for all its wanted values (they were read
        again per value: quadratic in a deep record times ``top``, review R5A-5); ``cancel`` is
        checked per field and record and every 256 tokens."""
        wanted: dict[int, dict[int, dict[tuple[int, int], list[_Top]]]] = {}
        for f, fin in zip(self.fields, finals):
            for tp in fin.top:
                if tp.token is None and tp.h is not None:
                    per_field = wanted.setdefault(self.scope.position(tp.first), {})
                    per_field.setdefault(f.index, {}).setdefault((tp.bits, tp.h), []).append(tp)
        if not wanted:
            return
        fields = {f.index: f for f in self.fields}
        cancel = self.cancel
        blocks = self.doc.iter_blocks(sorted(wanted), cancel=cancel)
        try:
            for blk in blocks:
                for k in range(len(blk.indices)):
                    reqs = wanted.get(int(blk.indices[k]))
                    if not reqs:
                        continue
                    for fi, pending in reqs.items():
                        check(cancel)
                        self._match(fields[fi], blk, k, pending)
        finally:
            blocks.close()

    def _match(self, f: _Field, blk: Any, k: int, pending: dict[tuple[int, int], list[_Top]]) -> None:
        """Give the wanted top values (``pending``: (bits, hash) -> tops) the first token of
        field ``f`` in record ``k`` of ``blk`` that has their hash; the tokens are read lazily and
        the reading stops once nothing is pending (a record with millions of values is streamed:
        its tokens were all kept at once -- review R5A-1)."""
        pos = int(blk.indices[k])
        try:
            tokens = self._tokens(f, blk, k, pos)
            if tokens is None:
                self._match_streamed(f, pos, pending)
                return
            for n, tok in enumerate(tokens):
                if n & 255 == 255:
                    check(self.cancel)
                _assign(pending, tok)
                if not pending:
                    return
        except Cancelled:
            raise
        except (CaptureLimitExceeded, ValueError) as exc:
            log.debug("stats: the value text of %s in #%d is not available: %s", f.field, pos, exc)
        except JsonReshapeError as exc:
            if exc.key != "stream.error.parse":
                raise

    def _tokens(self, f: _Field, blk: Any, k: int, pos: int) -> Iterable[bytes] | None:
        """The tokens of field ``f`` in record ``k`` of ``blk`` (document order), lazily; ``None``
        when the record is to be streamed (huge, one :meth:`_big_records` streams, or one the
        fast paths cannot read)."""
        member = self.member
        if f.is_key:
            key = self.doc.record_key(pos)
            return [] if key is None else [_dump(key)]
        if blk.huge:
            return None
        s, e = blk.span(k)
        if self.multi_extract and e - s > self.stream_values and blk.data.count(b",", s, e) > self.stream_values:
            return None
        if f.strategy == "extract" and e - s <= PARSE_MAX_BYTES * 64:
            if f.single_extractor is None:
                f.single_extractor = self.extractor_cls([f.pattern.to_core()],   # type: ignore[union-attr]
                                                        member_spans=member)
            try:
                q = _quads(f.single_extractor.extract(blk.data, blk.spans[16 * k:16 * k + 16]))
            except ValueError:
                q = None
            if q is not None:
                data = blk.data
                return (data[q[j + 2]:q[j + 3]] for j in range(0, len(q), 4))
        if e - s <= PARSE_MAX_BYTES:
            raw = blk.data[s:e]
            try:
                if member:
                    raw = raw[_split_member(raw)[1]:]
                value = codec.loads(raw)
            except _DECODE_ERRORS:
                return None
            return (_dump(v) for v in f.pattern.values(value))   # type: ignore[union-attr]
        return None

    def _match_streamed(self, f: _Field, pos: int, pending: dict[tuple[int, int], list[_Top]]) -> None:
        """:meth:`_match` for a streamed record: a probe hashes the scalars as they stream by and
        stops the stream when nothing is pending; containers (and records with duplicate keys)
        are read with ``stream_values`` within the capture budget."""
        span = self.doc.record_span(pos)
        probe = _MatchProbe(pending, self.cancel)
        try:
            stream_probe(self.doc, span, [f.pattern], {0: [probe]}, self.member,   # type: ignore[list-item]
                         cancel=self.cancel)
        except _Matched:
            return
        except DuplicateKeys:
            probe.containers = 1
        if pending and probe.containers:
            vals = stream_values(self.doc, span, [f.pattern], self.member, max_bytes=self.capture,
                                 cancel=self.cancel)[0]
            for v in vals:
                _assign(pending, _dump(v))
                if not pending:
                    return

    def _result(self, f: _Field, acc: _FieldAcc, fin: _Final, elapsed: float) -> FieldStats:
        p = self.pres[f.index]
        agg = acc.agg
        types = agg.types
        pos = self.scope.position
        tops = []
        for tp in fin.top:
            value, text = _decode_token(tp.token)
            tops.append(ValueCount(value, text, tp.count, tp.records, pos(tp.first), tp.error))
        number = None
        rounded = False
        if types[T_NUMBER] and agg.nmin is not None and agg.nmax is not None:
            total, not_rounded = agg.sum.total()
            rounded = not not_rounded
            skipped = agg.sum.skipped
            summed = types[T_NUMBER] - skipped     # the mean is over the numbers in the sum
            mean_text = _mean_text(_MEAN.divide(total, Decimal(summed))) if summed > 0 else "—"
            number = NumberStats(
                count=types[T_NUMBER], integers=agg.integers, min_text=_text(agg.nmin[3]),
                max_text=_text(agg.nmax[3]), min_first=pos(agg.nmin[1]), max_first=pos(agg.nmax[1]),
                sum_text=_sum_text(total), mean_text=mean_text, exact_sum=not_rounded and not skipped,
                sum_skipped=skipped)
        string = StringStats(agg.slo, agg.shi, agg.empty) if types[T_STRING] else None
        ordered = {TYPE_NAMES[i]: types[i] for i in sorted(range(6), key=lambda i: (-types[i], i)) if types[i]}
        keys: list[str] = []
        params: list[dict[str, Any]] = []
        if f.is_key and not self.member:
            if p.scanned:
                keys.append("stats.warn.no_record_keys")
                params.append({"count": p.scanned})
        elif p.scanned and not p.values:
            keys.append("stats.warn.field_absent")
            params.append({"field": f.field, "count": p.scanned})
        if p.too_large:
            keys.append("stats.warn.too_large")
            params.append({"count": p.too_large, "record": p.first_too_large, "size": format_bytes(self.capture)})
        if p.unreadable:
            keys.append("stats.warn.unreadable")
            params.append({"count": p.unreadable, "record": p.first_unreadable})
        if acc.uncounted:
            keys.append("stats.warn.uncounted")
            params.append({"count": acc.uncounted, "record": p.first_uncounted, "size": format_bytes(self.capture)})
        if not fin.distinct_exact:
            keys.append("stats.warn.approximate")
            params.append({"rse": _pct_text(HLL_RSE * 100, 1), "bound": fin.bound})
        if number is not None and number.sum_skipped:
            keys.append("stats.warn.sum_skipped")
            params.append({"count": number.sum_skipped})
        if number is not None and rounded:
            keys.append("stats.warn.sum_approximate")
            params.append({"digits": SUM_DIGITS_MAX})
        return FieldStats(
            field=f.field, path=f.path, is_key=f.is_key, scanned=p.scanned, skipped=p.too_large + p.unreadable,
            base=p.base, present=p.present, missing=max(0, p.base - p.present), values=p.values,
            nulls=types[T_NULL], types=ordered, distinct=fin.distinct, distinct_exact=fin.distinct_exact,
            distinct_rse=fin.rse, top=tops, top_exact=fin.top_exact, top_bound=fin.bound, number=number,
            string=string, arr_len=(agg.alo, agg.ahi) if agg.ahi >= 0 else None,
            obj_keys=(agg.klo, agg.khi) if agg.khi >= 0 else None, gen=getattr(self.doc, "gen", None),
            elapsed=elapsed, warnings=tuple(keys), warning_params=tuple(params), uncounted=acc.uncounted,
            multi=f.multi, context=f.context_text, number_hist=agg.hist if types[T_NUMBER] else None,
            length_hist=agg.lens if types[T_STRING] else None)

    def cleanup(self) -> None:
        for runs in self.registry:
            runs.cleanup()
        self.registry.clear()


def _contexts_holding(q: array, cj: Any, js: Any) -> int:
    """The contexts (spans of ``cj``) holding at least one value of ``js`` (both quad offsets
    of one chunk): contexts never nest, so a value belongs to the last context starting at or
    before it, when it ends after it."""
    ctx: dict[int, tuple[list[int], list[int]]] = {}
    for j in cj:
        k = q[j]
        c = ctx.get(k)
        if c is None:
            c = ctx[k] = ([], [])
        c[0].append(q[j + 2])
        c[1].append(q[j + 3])
    for k, (starts, ends) in list(ctx.items()):
        if any(starts[i] > starts[i + 1] for i in range(len(starts) - 1)):
            pairs = sorted(zip(starts, ends))
            ctx[k] = ([s for s, _ in pairs], [e for _, e in pairs])
    marked: set[tuple[int, int]] = set()
    for j in js:
        k = q[j]
        c = ctx.get(k)
        if c is None:
            continue
        s = q[j + 2]
        i = bisect.bisect_right(c[0], s) - 1
        if i >= 0 and s < c[1][i]:
            marked.add((k, i))
    return len(marked)


# ----------------------------------------------------------------------------------
# texts
# ----------------------------------------------------------------------------------
def _text(tok: Any) -> str:
    return tok.decode("utf-8", "replace") if isinstance(tok, (bytes, bytearray)) else str(tok)


def _value_text(value: Any) -> str:
    """Display text of a value: ``schema.example_text`` for scalars (strings quoted and escaped,
    exact number texts), compact JSON for containers; control / bidi characters as ``\\uXXXX``;
    cut to :data:`TEXT_CHARS` characters with ``…``."""
    text = example_text(value, TEXT_CHARS)
    if text is not None:
        return text
    try:
        text = codec.dumps(value).decode("utf-8", "replace")
    except (TypeError, ValueError):  # pragma: no cover - values come from codec
        text = repr(value)
    text = _escape_display(text)
    return text if len(text) <= TEXT_CHARS else text[:TEXT_CHARS - 1] + ELLIPSIS


def _decode_token(token: bytes | None) -> tuple[Any, str]:
    """(value, display text) of a value's JSON token (``MISSING`` and a cut raw text for
    tokens over :data:`VALUE_DECODE_BYTES`; ``MISSING, "…"`` without a token)."""
    if token is None:
        return MISSING, ELLIPSIS
    if len(token) > VALUE_DECODE_BYTES:
        raw = token[:4 * TEXT_CHARS].decode("utf-8", "ignore")
        raw = _escape_display(" ".join(raw.split()))
        return MISSING, raw[:TEXT_CHARS - 1] + ELLIPSIS
    try:
        value = codec.loads(token)
    except _DECODE_ERRORS:   # pragma: no cover - tokens of validated records
        return MISSING, ELLIPSIS
    return value, _value_text(value)


def _plain(d: Decimal) -> str:
    """JSON number text of a finite Decimal: plain digits for moderate exponents, else the
    scientific form (``1E+400``)."""
    if d.is_zero():
        return "0"
    adj = d.adjusted()
    if -7 <= adj < 40:
        text = format(d, "f")
        return text
    return str(d)


def _sum_text(total: Decimal) -> str:
    return _plain(total)


def _mean_text(mean: Decimal) -> str:
    """15 significant digits, trailing zeros removed."""
    if mean.is_zero():
        return "0"
    r = _ROUND.plus(mean).normalize(_ROUND)
    return _plain(r)


def _pct_text(value: float, digits: int = 1) -> str:
    """``12.5`` (English) / ``12,5`` (Uzbek)."""
    text = f"{value:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",") if space_grouped() else text


def _count_text(n: int) -> str:
    text = f"{int(n):,}"
    return text.replace(",", " ") if space_grouped() else text


def _share(part: int, whole: int) -> str:
    if not whole:
        return "0%"
    x = part * 100.0 / whole
    if part and x < 0.1:
        return "<" + _pct_text(0.1) + "%"
    if part != whole and x > 99.9:
        return ">" + _pct_text(99.9) + "%"
    return _pct_text(x) + "%"


def _tn(key: str, n: int, **params: Any) -> str:
    """``t(key)`` with ``<key>.one`` for a count of 1 (English singular)."""
    return t(key + ".one" if n == 1 else key, **params)


def format_stats(stats: Sequence[FieldStats], *, records: int | None = None) -> str:
    """Localized plain-text blocks of field statistics (the CLI ``stats`` output; map_stats §6):
    field, presence, types, distinct values, numbers, lengths, the top values and the
    warnings."""
    lines: list[str] = []
    for fs in stats:
        if lines:
            lines.append("")
        nrec = fs.scanned if records is None else records
        head = _tn("stats.text.field", nrec, field=fs.field, records=_count_text(nrec),
                   seconds=_pct_text(fs.elapsed, 1))
        lines.append(head)
        if fs.context != "$":
            lines.append("  " + _tn("stats.text.context", fs.base, context=fs.context, count=_count_text(fs.base)))
        lines.append("  " + t("stats.text.presence", present=_count_text(fs.present),
                              present_pct=_share(fs.present, fs.base), nulls=_count_text(fs.nulls),
                              nulls_pct=_share(fs.nulls, fs.values), missing=_count_text(fs.missing),
                              missing_pct=_share(fs.missing, fs.base)))
        if fs.types:
            parts = [f"{t('schema.type.' + name)} {_share(n, fs.values)}" for name, n in fs.types.items()]
            lines.append("  " + t("stats.text.types", types=" · ".join(parts)))
        if fs.distinct_exact:
            lines.append("  " + t("stats.text.distinct_exact", count=_count_text(fs.distinct)))
        else:
            lines.append("  " + t("stats.text.distinct_approx", count=_count_text(fs.distinct),
                                  rse=_pct_text((fs.distinct_rse or 0.0) * 100, 1)))
        nm = fs.number
        if nm is not None:
            lines.append("  " + t("stats.text.numbers", count=_count_text(nm.count), integers=_count_text(nm.integers),
                                  min=nm.min_text, max=nm.max_text, mean=nm.mean_text, sum=nm.sum_text))
        if fs.string is not None:
            lines.append("  " + t("stats.text.strings", min=fs.string.min_len, max=fs.string.max_len,
                                  empty=_count_text(fs.string.empty)))
        if fs.arr_len is not None:
            lines.append("  " + t("stats.text.arrays", min=fs.arr_len[0], max=fs.arr_len[1]))
        if fs.obj_keys is not None:
            lines.append("  " + t("stats.text.objects", min=fs.obj_keys[0], max=fs.obj_keys[1]))
        if fs.top:
            key = "stats.text.top_exact" if fs.top_exact else "stats.text.top_approx"
            lines.append("  " + _tn(key, len(fs.top), count=len(fs.top), bound=_count_text(fs.top_bound)))
            width = max(len(v.text) for v in fs.top)
            width = min(width, 40)
            for i, v in enumerate(fs.top, 1):
                text = v.text if len(v.text) <= 40 else v.text[:39] + ELLIPSIS
                row = f"    {i:>3}. {text:<{width}}  {_count_text(v.count):>12}  {_share(v.count, fs.values):>7}"
                if fs.multi:
                    row += "  " + _tn("stats.text.records", v.records, count=_count_text(v.records))
                lines.append(row)
        for w in fs.warning_texts():
            lines.append("  ! " + w)
    return "\n".join(lines)


# ----------------------------------------------------------------------------------
# progress
# ----------------------------------------------------------------------------------
def _data_bytes(doc: Any) -> int:
    if getattr(doc, "edited", False):
        return max(1, int(doc.effective_bytes()))
    return max(1, int(doc.data_size) - int(doc.info.data_start))


# ----------------------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------------------
def field_stats(doc: Any, fields: str | Sequence[str], source: Any = None, *, top: int = DEFAULT_TOP,
                exact: bool = False, sample: int | None = None, memory_limit: int | None = None,
                progress: Progress | None = None, cancel: CancelToken | None = None,
                on_partial: Callable[[list[FieldStats]], Any] | None = None,
                strategy: str = "auto") -> list[FieldStats]:
    """Statistics of ``fields`` over the records of ``source`` (module docstring).

    ``fields``: one path pattern or several (``"contact.address.city"``, ``"orders[*].sum"``,
    ``"..name"``, ``"$"``, ``"$key"``); several fields share one pass. ``source``: ``None`` (all
    records), a RecordSet / range / sequence / iterable of positions; ``top``: the most frequent
    values to list (0..:data:`TOP_MAX`); ``exact``: no sketches (spilled runs instead);
    ``sample``: examine an evenly spread sample of that many records of the scope;
    ``memory_limit``: bytes for the exact counting (default ``Settings.memory_limit_bytes //
    8``); ``on_partial``: called with live snapshots (``FieldStats.partial``) about every second,
    from the calling thread; ``strategy``: ``"auto"`` / ``"extract"`` / ``"parse"`` (same
    results). Returns one :class:`FieldStats` per requested field, in order (a field named twice
    gives the same object twice).

    Raises ``UserInputError`` (bad arguments), ``PathSyntaxError``,
    ``EditError("edits.error.stale_scope")``, ``Cancelled`` and ``FileChangedError``."""
    started = time.perf_counter()
    pinned = getattr(doc, "pinned", None)
    if callable(pinned):
        doc = pinned()
    specs, order = _parse_fields(fields)
    top = _check_top(top)
    sample = _check_sample(sample)
    strategy = _check_strategy(strategy)
    settings = getattr(doc, "settings", None) or get_settings()
    budget = _check_budget(memory_limit, settings)
    gen = getattr(doc, "gen", None)
    src_gen = getattr(source, "gen", None) if source is not None else None
    if src_gen is not None and src_gen != gen:
        raise EditError("edits.error.stale_scope")
    src = source
    if isinstance(src, RecordSet) and src._is_base_of(doc):
        src = None
    name = doc.source_path.name
    total = _data_bytes(doc)
    scan_start = 0.0
    if progress is not None:
        if not progress.message:
            progress.set_message(t("stats.progress", name=name))
        if progress.total is None:
            progress.set_total(total)
    if src is None and not doc.index_complete:
        doc.build_index(progress=progress.child(0.0, INDEX_SHARE) if progress is not None else None, cancel=cancel)
        scan_start = INDEX_SHARE
    check(cancel)
    scope = _Scope(doc, src, sample, cancel)
    scan_total: int | None = total
    if scope.seq is not None:
        count = max(1, int(doc.record_count))
        scan_total = max(1, int(total * min(1.0, scope.n / count)))
    scan_p = progress.child(scan_start, 1.0 - FINISH_SHARE, total=scan_total) if progress is not None else None
    engine = _Engine(doc, specs, scope, budget=budget, exact=bool(exact), top=top, settings=settings,
                     strategy=strategy, cancel=cancel, on_partial=on_partial, order=order, started=started)
    try:
        engine.scan(scan_p)
        check(cancel)
        fin_p = None
        if progress is not None:
            progress.set_message(t("stats.progress.finish", name=name))
            fin_p = progress.child(1.0 - FINISH_SHARE, 1.0, total=1000)
        results = engine.finish(fin_p)
    finally:
        engine.cleanup()
    ensure = getattr(doc, "ensure_unchanged", None)
    if ensure is not None:
        ensure()     # results of a file another program changed meanwhile are not reported
    if progress is not None:
        progress.finish()
    log.info("stats of %s: %s over %d records (%.2f s, %d overflow(s))", name, ", ".join(f.field for f in specs),
             scope.n, time.perf_counter() - started, engine.accset.overflows)
    return [results[i] for i in order]
