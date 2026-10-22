r"""Parquet and Arrow IPC / Feather export (docs/ARCHITECTURE.md §8; stage 7): the ``parquet``
format of :func:`jsonreshape.writer.export` (``.parquet``) and the ``feather`` / ``arrow`` formats
(``.feather`` / ``.arrow`` -- one format under two names: the Arrow IPC file format, which Feather v2
is), written with the optional ``pyarrow`` package -- ``pip install "jsonreshape[parquet]"``. The
program imports and works without it; such an export then raises ``writer.error.parquet_missing`` /
``writer.error.feather_missing`` (naming the pip command) before any work. :mod:`jsonreshape.writer`
registers this module in its ``_EXPORTERS`` and builds the rows; the texts live in the ``writer``
section of :mod:`jsonreshape.i18n`.

Both formats share everything below -- the columns, the cell types, the type decision with its
two-pass restart, the row groups and the worker processes -- through :func:`_export_columnar`; only
the write backend differs (:class:`_ParquetBackend`: ``ParquetWriter`` row groups; :class:`_ArrowBackend`:
``pyarrow.ipc.new_file``, one record batch per row group) and so do the message keys
(``writer.*.parquet*`` / ``writer.*.feather*``). Feather compression is
``ExportOptions.feather_compression``: ``zstd`` (default), ``lz4`` or ``none`` (CLI
``--feather-compression``); ``.feather.gz`` / ``.arrow.gz`` are refused like Parquet (the IPC buffers
compress themselves).

Columns
=======
Exactly the CSV/XLSX columns of :mod:`jsonreshape.writer` ("CSV and XLSX"): ``columns``
(path patterns, ``$key``, ``$index``, ``$``) or the automatic columns of the scan
(``discover_columns``: nested names ``a.b.c``, maps pooled into ``<path>.*``, the depth and
count limits), ``array_mode="join"`` / ``"rows"``, ``explode`` -- so the three table formats
of one export stay consistent (the same header, the same rows). ``.parquet.gz`` is refused
like XLSX (``writer.error.gzip_format``: Parquet compresses its own pages).

Cells and types
===============
A column has one Arrow type for the whole file, decided from its values (nulls -- JSON
``null`` and missing values -- are null in any type and do not count):

* ``int64`` -- every value is an integer that fits int64 (``-0`` is 0);
* ``double`` -- every value is a number a double holds exactly as written: ``0.1``, ``1.10``
  (1.1), ``1E3``, ``-0.0``, ``3.141592653589793``, and integers up to 2**53 in magnitude
  among them. The rule of the SQLite export (``writer._exact_double``: the token's decimal
  value equals the double's); a number with more digits than a double keeps
  (``123456789.123456789``), ``1e400`` or an integer beyond 2**53 next to doubles makes the
  column ``string`` instead -- digits are never lost (the number-precision rule). This is
  stricter than XLSX's "15 significant digits" where the double would be inexact and looser
  where it is exact (``3.141592653589793`` is a typed double here, text in Excel);
* ``bool`` -- every value is ``true``/``false``;
* ``string`` -- strings; and any column whose values mix types (a string among numbers,
  booleans among numbers, integers beyond int64): the non-string values are written as their
  CSV text -- the exact JSON number text, ``true``/``false``. A column that holds only
  numbers but became text for lack of an exact type is counted in
  ``writer.warn.parquet_number_text`` (the columns are named);
* an array in join mode is one string: the elements joined with ``join_sep`` as in CSV
  (container elements as compact JSON; an empty array is ``""``); an object that is not
  expanded into columns (a map's pooled tail, a deep object) is its compact JSON text;
  ``$key`` is a string (null for records that are not object members), ``$index`` an int64;
* a column that holds no value but null is ``string`` (all null).

How the types are decided
=========================
One pass in the common case. The rows are built exactly as for CSV (the same
``_RowBuilder``; in worker processes for large exports, see below) and the first
:data:`TYPE_ROWS` = 65 536 rows (fewer for very wide tables: at most
``2 * memory_limit_bytes // 512`` cells, at least :data:`TYPE_MIN_ROWS` = 1024 rows) are kept; the
schema is fixed from them by the rules
above and the file is written from there on. A later value that does not fit its column's
type -- a float in an ``int64`` column, a string in a ``bool`` column, an integer beyond int64,
a non-string value in a column that was all null -- cannot be written (a Parquet file has one
schema): the export then starts over in **two passes**: a pass over every row that only
collects the types, then the writing pass with the schema decided from all rows. The
temporary file of the aborted attempt is removed, the counters and warnings of the attempt
are dropped, ``writer.warn.parquet_restarted`` names the column and the record (0-based)
whose value forced it, and progress continues (its total grows by two passes). The result is
the same file the two-pass mode writes directly: the prefix decision and the conflict rule are
such that, when no conflict occurs, the prefix's type IS the type all rows give (a string-forcing
mix stays string however the data continues; an all-null prefix is string only provisionally
and any later non-string value restarts) -- so the file is a function of the rows alone, never
of how they were batched. The alternative -- an error naming the column -- was rejected: the user
has no way to force a column's type, and the data never loses anything in two passes. (A value
that does not fit in the second pass -- only possible when the source changed between the
passes -- is ``writer.error.parquet_type``; the commit check reports the change itself.)

Writing
=======
* ``pyarrow.parquet.ParquetWriter`` over a temporary file next to the target
  (:func:`jsonreshape.fileio.atomic_path`; the file appears atomically, an error or a
  cancellation leaves no temporary file and an existing target untouched; the source is
  checked with ``ensure_unchanged`` and the target with ``refuse_edited_targets`` right before
  the commit); Parquet format version 2.6, dictionary encoding and statistics (pyarrow's
  defaults); compression ``ExportOptions.parquet_compression``: ``zstd`` (default), ``snappy``,
  ``gzip`` or ``none`` (CLI ``--parquet-compression``).
* Row groups: the converted rows are kept as Arrow record batches (compact) and a row group is
  written every ``G`` rows, ``G`` = the rows that make about :data:`ROW_GROUP_BYTES` = 64 MiB
  estimated from the first :data:`TYPE_ROWS` rows (strings: their length plus an offset,
  numbers 8 bytes), at most :data:`ROW_GROUP_MAX_ROWS` = 1 048 576 rows and at least
  :data:`ROW_GROUP_MIN_ROWS` = 1024; the last group holds the rest. ``G`` depends on the data
  alone, and every row group is written as one contiguous chunk (``combine_chunks``: Arrow's
  page splitting depends on the chunks it is given), so the files of the one-thread and the
  worker exports are byte for byte the same. Only when the rows grow far beyond the estimate
  (more than :data:`PENDING_MAX_BYTES` = 512 MiB of batches waiting) a group is cut early to
  bound the memory -- the data is the same, the row-group boundaries then are not.
* Memory: the type buffer (at most TYPE_ROWS rows of Python cells, bounded by the cell
  budget), the batches of one row group (Arrow arrays) and one row batch; rows are never
  collected whole.
* Rows in worker processes exactly as CSV/XLSX (``export(..., workers=None)``:
  ``bulk.auto_workers`` from 64 MiB of records; ``workers=1`` keeps everything in the calling
  thread): :class:`jsonreshape.writer._RowsPool` with the jobs returning typed rows of
  :class:`_ParquetCells` (the XLSX way); the calling thread converts them to Arrow arrays in
  block order and writes; the type pass of the two-pass mode uses the pool too.
  ``ExportReport.workers`` tells. A hidden trailing ``$index`` column (never written) carries
  the record number of every row for the restart notice.
* Progress and cancellation as CSV (``writer.progress.export``; the type pass reports
  ``writer.progress.parquet_types``); ``cancel`` is checked for every block, every 1024
  records, every write pyarrow makes to the file and before every row group.
* Explicit columns that found nothing: ``writer.warn.columns_not_found``; huge records and
  records that cannot be decoded in memory: as CSV (streamed with explicit columns, skipped with
  automatic ones). ``ExportReport.rows`` = data rows, ``columns`` = the header. A lone
  surrogate (from a ``\ud800`` escape; Arrow strings are UTF-8) is written as the text
  ``\ud800`` in values and column names, as CSV and XLSX write it.
* A pyarrow error while writing is reported as ``writer.error.parquet`` (``detail``).
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from typing import Any, Callable, Iterator

from .codec import JsonFloat, JsonInt
from .errors import ExportError
from .fileio import atomic_path, long_path, os_error
from .paths import MISSING
from .tasks import check
from .writer import (
    INDEX_COLUMN,
    WRITE_BUFFER,
    _SURROGATE_RE,
    _Column,
    _Ctx,
    _escape_surrogates,
    _iter_row_batches,
    _json_text,
    _names_text,
    _pool_workers,
    _RowBuilder,
    _RowsPool,
    _TextCells,
    _warn_unfound,
)

__all__ = ["FEATHER_COMPRESSIONS", "PARQUET_COMPRESSIONS", "ROW_GROUP_BYTES", "ROW_GROUP_MAX_ROWS", "TYPE_ROWS",
           "export_feather", "export_parquet", "pyarrow_available", "pyarrow_ipc_available", "require_pyarrow",
           "require_pyarrow_ipc"]

PARQUET_COMPRESSIONS = ("zstd", "snappy", "gzip", "none")
FEATHER_COMPRESSIONS = ("zstd", "lz4", "none")   # Arrow IPC buffer compression (ExportOptions.feather_compression)
TYPE_ROWS = 65536                     # rows kept to decide the column types (fewer for wide tables) ...
TYPE_CELL_FACTOR = 2                  # ... at most this many times the cell budget (memory_limit_bytes // 512) ...
TYPE_MIN_ROWS = 1024                  # ... but at least this many
ROW_GROUP_BYTES = 64 << 20            # a row group holds about this many estimated bytes ...
ROW_GROUP_MAX_ROWS = 1 << 20          # ... at most this many rows ...
ROW_GROUP_MIN_ROWS = 1024             # ... and at least this many
PENDING_MAX_BYTES = 512 << 20         # batches waiting for a row group beyond this: cut early (memory bound)
STRING_OVERHEAD = 4                   # estimated bytes of a string cell besides its characters (offsets)
SAFE_INT = 1 << 53                    # integers up to this magnitude are exact doubles
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1
_NULL = type(None)
_INT_TYPES = frozenset({int, JsonInt})
_FLOAT_TYPES = frozenset({float, JsonFloat, Decimal})
_NUMBER_TYPES = _INT_TYPES | _FLOAT_TYPES
_PLAIN_TYPES = frozenset({str, int, float, bool, _NULL})      # cells kept as they are by _ParquetCells.one
_INT_OR_NULL = _INT_TYPES | {_NULL}
_NUMBER_OR_NULL = _NUMBER_TYPES | {_NULL}
_BOOL_OR_NULL = frozenset({bool, _NULL})
_STR_OR_NULL = frozenset({str, _NULL})


# ----------------------------------------------------------------------------------
# pyarrow
# ----------------------------------------------------------------------------------
def pyarrow_available() -> bool:
    """Whether ``pyarrow`` (with its Parquet module) can be imported."""
    try:
        import pyarrow.parquet  # noqa: F401
    except ImportError:
        return False
    return True


def require_pyarrow() -> tuple[Any, Any]:
    """``(pyarrow, pyarrow.parquet)``, or ``ExportError`` ``writer.error.parquet_missing``."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ExportError("writer.error.parquet_missing", detail=str(exc)) from None
    return pa, pq


def pyarrow_ipc_available() -> bool:
    """Whether ``pyarrow`` (with its IPC module, for Arrow / Feather) can be imported."""
    try:
        import pyarrow.ipc  # noqa: F401
    except ImportError:
        return False
    return True


def require_pyarrow_ipc() -> Any:
    """``pyarrow`` (its ``ipc`` module loaded), or ``ExportError`` ``writer.error.feather_missing``."""
    try:
        import pyarrow as pa
        import pyarrow.ipc  # noqa: F401
    except ImportError as exc:
        raise ExportError("writer.error.feather_missing", detail=str(exc)) from None
    return pa


# ----------------------------------------------------------------------------------
# write backends: Parquet row groups / Arrow IPC record batches
# ----------------------------------------------------------------------------------
class _ParquetBackend:
    """``pyarrow.parquet.ParquetWriter``; a row group per :meth:`write_group`."""

    def __init__(self, pq: Any, compression: str) -> None:
        self._pq = pq
        self._compression = compression

    def open(self, sink: Any, schema: Any) -> Any:
        return self._pq.ParquetWriter(sink, schema, compression=self._compression)

    @staticmethod
    def write_group(writer: Any, part: Any, g: int) -> None:
        writer.write_table(part, row_group_size=g)


class _ArrowBackend:
    """The Arrow IPC file format (``pyarrow.ipc.new_file``; ``.arrow``, and Feather v2 is the same
    file): one record batch per row group -- ``part`` is one contiguous chunk, so the batch boundaries
    depend on the data alone and the one-thread and worker files are byte for byte the same."""

    def __init__(self, pa: Any, options: Any) -> None:
        self._pa = pa
        self._options = options

    def open(self, sink: Any, schema: Any) -> Any:
        return self._pa.ipc.new_file(sink, schema, options=self._options)

    @staticmethod
    def write_group(writer: Any, part: Any, g: int) -> None:
        writer.write_table(part)


# ----------------------------------------------------------------------------------
# cells
# ----------------------------------------------------------------------------------
class _ParquetCells:
    """Typed Parquet cell values: ``None`` (null), ``str``, ``bool``, ``int``/``JsonInt``,
    ``float``/``JsonFloat`` (the number objects as decoded, so the type decision can look at
    them); arrays and objects as the CSV texts (joined / compact JSON)."""

    empty = None

    def __init__(self, sep: str) -> None:
        text = _TextCells(sep)
        text_many = text.many
        plain = _PLAIN_TYPES

        def one(v: Any) -> Any:
            tv = type(v)
            if tv in plain:
                return v
            if v is MISSING:
                return None
            if tv is list:
                return text_many(v)
            if tv is dict:
                return _json_text(v)
            return v   # JsonInt, JsonFloat, Decimal

        def convert(row: list[Any]) -> list[Any]:
            return [x if (tx := type(x)) is str or tx is int or tx is float or x is None else one(x) for x in row]

        self.one: Callable[[Any], Any] = one
        self.many: Callable[[list[Any]], Any] = text_many
        self.convert: Callable[[list[Any]], list[Any]] = convert
        self.text_one: Callable[[Any], str] = text.one

    @staticmethod
    def key(key: str | None) -> Any:
        return key

    @staticmethod
    def index(index: int) -> Any:
        return int(index)


def _exact(v: Any) -> bool:
    """A ``JsonFloat`` / ``Decimal`` whose decimal value a double holds exactly (the SQLite rule)."""
    try:
        f = float(v)
    except (OverflowError, ValueError):
        return False
    if not isfinite(f):
        return False
    try:
        return Decimal(v.raw if type(v) is JsonFloat else str(v)) == Decimal(repr(f))
    except InvalidOperation:  # pragma: no cover - codec number texts are valid
        return False


class _ColumnTypes:
    """What the values of one column were so far: the Python types seen and, of the numbers,
    whether an integer lies outside int64 (``big``), beyond 2**53 (``wide``: exact in no double) or a
    float is not exactly a double (``inexact``)."""

    __slots__ = ("kinds", "big", "wide", "inexact")

    def __init__(self) -> None:
        self.kinds: set[type] = set()
        self.big = False
        self.wide = False
        self.inexact = False

    def add(self, vals: Any) -> None:
        kinds = set(map(type, vals))
        self.kinds |= kinds
        if kinds & _INT_TYPES and not self.big:
            for v in vals:
                tv = type(v)
                if (tv is int or tv is JsonInt) and not -SAFE_INT <= v <= SAFE_INT:
                    self.wide = True
                    if not _INT64_MIN <= v <= _INT64_MAX:
                        self.big = True
                        break
        if (JsonFloat in kinds or Decimal in kinds) and not self.inexact:
            for v in vals:
                tv = type(v)
                if (tv is JsonFloat or tv is Decimal) and not _exact(v):
                    self.inexact = True
                    break

    def decide(self) -> tuple[str, bool, bool]:
        """``(arrow type, provisional, numbers_as_text)``: the type of the column from what was seen;
        ``provisional``: nothing but null so far (``string`` until a value says otherwise);
        ``numbers_as_text``: the column holds numbers only but no exact type."""
        k = self.kinds - {_NULL}
        if not k:
            return "string", True, False
        if str in k:
            return "string", False, False
        numbers = k <= _NUMBER_TYPES
        if self.big or self.inexact:
            return "string", False, numbers
        if bool in k:
            return ("bool" if k == {bool} else "string"), False, False
        if k & _FLOAT_TYPES:
            if k & _INT_TYPES and self.wide:
                return "string", False, True
            return "double", False, False
        return "int64", False, False


def _misfit(vals: Any, kinds: set[type], kind: str, provisional: bool) -> int:
    """``-1`` when every value of ``vals`` (Python types ``kinds``) can be written in a column of ``kind``,
    else the position of the first one that cannot (``provisional``: an all-null string column takes
    strings only -- any other value means the type decided from all rows is another one)."""
    if kind == "string":
        if not provisional or kinds <= _STR_OR_NULL:
            return -1
        return next(i for i, v in enumerate(vals) if v is not None and type(v) is not str)
    if kind == "int64":
        if not kinds <= _INT_OR_NULL:
            return next(i for i, v in enumerate(vals) if v is not None and type(v) not in _INT_TYPES)
        if JsonInt in kinds:
            for i, v in enumerate(vals):
                if type(v) is JsonInt and not _INT64_MIN <= v <= _INT64_MAX:
                    return i
        return -1       # (a plain int beyond int64 is caught by the conversion: OverflowError)
    if kind == "double":
        if not kinds <= _NUMBER_OR_NULL:
            return next(i for i, v in enumerate(vals) if v is not None and type(v) not in _NUMBER_TYPES)
        if kinds & _INT_TYPES or JsonFloat in kinds or Decimal in kinds:
            for i, v in enumerate(vals):
                tv = type(v)
                if tv is int or tv is JsonInt:
                    if not -SAFE_INT <= v <= SAFE_INT:
                        return i
                elif (tv is JsonFloat or tv is Decimal) and not _exact(v):
                    return i
        return -1
    if not kinds <= _BOOL_OR_NULL:
        return next(i for i, v in enumerate(vals) if v is not None and type(v) is not bool)
    return -1


class _Restart(Exception):
    """A value does not fit the fixed schema: the export starts over in two passes."""

    def __init__(self, column: str, index: int) -> None:
        super().__init__(column, index)
        self.column = column
        self.index = index


def _row_bytes(rows: list[list[Any]]) -> int:
    """Estimated Parquet-side bytes of ``rows`` (the hidden index column excluded)."""
    n = 0
    for row in rows:
        for v in row[:-1]:
            tv = type(v)
            if tv is str:
                n += len(v) + STRING_OVERHEAD
            elif v is None:
                n += 1
            else:
                n += 8
    return n


def _group_rows(nbytes: int, rows: int) -> int:
    """Rows per row group for rows of ``nbytes / rows`` estimated bytes."""
    if rows <= 0 or nbytes <= 0:
        return ROW_GROUP_MAX_ROWS
    per_row = max(1, nbytes // rows)
    return max(ROW_GROUP_MIN_ROWS, min(ROW_GROUP_MAX_ROWS, ROW_GROUP_BYTES // per_row))


# ----------------------------------------------------------------------------------
# the rows -> Arrow batches -> row groups
# ----------------------------------------------------------------------------------
class _CancelFile:
    """The file pyarrow writes to: ``cancel`` is checked for every write."""

    def __init__(self, f: Any, cancel: Any) -> None:
        self._f = f
        self._cancel = cancel

    def write(self, data: Any) -> int:
        check(self._cancel)
        return self._f.write(data)

    def tell(self) -> int:
        return self._f.tell()

    def flush(self) -> None:
        self._f.flush()

    def close(self) -> None:
        pass            # the exporter closes the file itself

    @property
    def closed(self) -> bool:
        return self._f.closed

    @staticmethod
    def writable() -> bool:
        return True

    @staticmethod
    def readable() -> bool:
        return False

    @staticmethod
    def seekable() -> bool:
        return False


class _Collector:
    """Where the rows of a Parquet export go (the ``out`` of :class:`writer._RowsPool` -- ``piece`` /
    ``rows`` / ``flush`` -- and of the one-thread loop; every row ends with the hidden record number).

    ``mode``: ``"types"`` -- only collect the column types (the first pass of the two-pass mode);
    ``"single"`` -- keep the first ``type_rows`` rows, decide the schema, write; ``"fixed"`` -- write
    with the ``kinds`` given (the second pass). :meth:`finish` writes the rest and closes the writer."""

    def __init__(self, ctx: _Ctx, pa: Any, backend: Any, names: list[str], cells: _ParquetCells, mode: str,
                 type_rows: int, f: Any = None, kinds: list[str] | None = None, group_rows: int | None = None) -> None:
        self.ctx = ctx
        self.pa = pa
        self.backend = backend
        self.names = names
        self.width = len(names)
        self.text_one = cells.text_one
        self.mode = mode
        self.type_rows = type_rows
        self.f = f
        self.types = [_ColumnTypes() for _ in names]
        self.kinds: list[str] = list(kinds) if kinds is not None else []
        self.provisional: list[bool] = [False] * self.width
        self.numbers_as_text: list[str] = []
        self.buffer: list[list[Any]] = []        # "single": the first type_rows rows
        self.seen = 0                           # rows seen ("types" mode)
        self.est_bytes = 0                      # estimated bytes of the first type_rows rows
        self.group_rows: int | None = group_rows
        self.schema: Any = None
        self.writer: Any = None
        self.pending: list[Any] = []            # Arrow record batches waiting for a row group
        self.pending_rows = 0
        self.pending_bytes = 0
        self.decided = mode == "fixed"
        if self.decided:
            self._open()

    # -- the rows ------------------------------------------------------------------------
    def piece(self, rows: list[list[Any]]) -> None:
        self.add(rows)

    def rows(self, rows: list[list[Any]]) -> None:
        self.add(rows)

    def flush(self) -> None:
        pass

    def add(self, rows: list[list[Any]]) -> None:
        if not rows:
            return
        if self.mode == "types":
            self._collect(rows)
            return
        if not self.decided:
            need = self.type_rows - len(self.buffer)
            if len(rows) < need:
                self.buffer.extend(rows)
                return
            head, rest = (rows, []) if len(rows) == need else (rows[:need], rows[need:])
            self.buffer.extend(head)
            self._decide_from_buffer()
            if not rest:
                return
            rows = rest
        self._convert(rows)

    def _collect(self, rows: list[list[Any]]) -> None:
        """Types pass: the types of every column, the size estimate of the first rows."""
        if self.seen < self.type_rows:
            take = rows if self.seen + len(rows) <= self.type_rows else rows[:self.type_rows - self.seen]
            self.est_bytes += _row_bytes(take)
        self.seen += len(rows)
        cols = list(zip(*rows))
        for j, types in enumerate(self.types):
            types.add(cols[j])

    def decide_all(self) -> tuple[list[str], int]:
        """Types pass: ``(kinds, group_rows)`` for the writing pass."""
        kinds = []
        for name, types in zip(self.names, self.types):
            kind, _provisional, numbers = types.decide()
            kinds.append(kind)
            if numbers:
                self.numbers_as_text.append(name)
        return kinds, _group_rows(self.est_bytes, min(self.seen, self.type_rows))

    def _decide_from_buffer(self) -> None:
        rows = self.buffer
        self.buffer = []
        if rows:
            self.est_bytes = _row_bytes(rows)
            cols = list(zip(*rows))
            for j, types in enumerate(self.types):
                types.add(cols[j])
        for j, (name, types) in enumerate(zip(self.names, self.types)):
            kind, provisional, numbers = types.decide()
            self.kinds.append(kind)
            self.provisional[j] = provisional
            if numbers:
                self.numbers_as_text.append(name)
        if self.group_rows is None:
            self.group_rows = _group_rows(self.est_bytes, len(rows))
        self.decided = True
        self._open()
        if rows:
            self._convert(rows)

    # -- Arrow ---------------------------------------------------------------------------
    def _open(self) -> None:
        pa = self.pa
        arrow = {"string": pa.string(), "int64": pa.int64(), "double": pa.float64(), "bool": pa.bool_()}
        self.schema = pa.schema([(name, arrow[kind]) for name, kind in zip(self.names, self.kinds)])
        self.writer = self.backend.open(_CancelFile(self.f, self.ctx.cancel), self.schema)

    def _array(self, vals: Any, kinds: set[type], kind: str) -> Any:
        pa = self.pa
        if kind == "string":
            if not kinds <= _STR_OR_NULL:
                text_one = self.text_one
                vals = [v if type(v) is str or v is None else text_one(v) for v in vals]
            try:
                return pa.array(vals, type=pa.string())
            except UnicodeEncodeError:
                # a lone surrogate (from a \ud800 escape): the text "\ud800", as CSV and XLSX write it
                vals = [_escape_surrogates(v) if type(v) is str and _SURROGATE_RE.search(v) else v for v in vals]
                return pa.array(vals, type=pa.string())
        if kind == "int64":
            return pa.array(vals, type=pa.int64())
        if kind == "double":
            if Decimal in kinds:
                vals = [float(v) if type(v) is Decimal else v for v in vals]
            return pa.array(vals, type=pa.float64())
        return pa.array(vals, type=pa.bool_())

    def _convert(self, rows: list[list[Any]]) -> None:
        pa = self.pa
        cols = list(zip(*rows))
        indices = cols[-1]
        arrays = []
        conflict: tuple[int, str] | None = None      # the first row (and its column) that does not fit
        for j in range(self.width):
            vals = cols[j]
            kinds = set(map(type, vals))
            kind = self.kinds[j]
            bad = _misfit(vals, kinds, kind, self.provisional[j])
            if bad < 0:
                try:
                    arrays.append(self._array(vals, kinds, kind))
                    continue
                except OverflowError:
                    bad = next((i for i, v in enumerate(vals) if v is not None and not _INT64_MIN <= v <= _INT64_MAX), 0)
            if conflict is None or bad < conflict[0]:
                conflict = (bad, self.names[j])
        if conflict is not None:
            # (the earliest row of the batch: the same record whatever the batches, so the notice is the same
            # with and without worker processes)
            raise _Restart(conflict[1], int(indices[conflict[0]]))
        batch = pa.RecordBatch.from_arrays(arrays, schema=self.schema)
        self.pending.append(batch)
        self.pending_rows += len(rows)
        self.pending_bytes += batch.nbytes
        g = self.group_rows or ROW_GROUP_MAX_ROWS
        while self.pending_rows >= g:
            self._write_group(g)
        if self.pending_bytes > PENDING_MAX_BYTES and self.pending_rows:    # rows far beyond the estimate
            self._write_group(self.pending_rows)

    def _write_group(self, g: int) -> None:
        pa = self.pa
        check(self.ctx.cancel)
        table = pa.Table.from_batches(self.pending, schema=self.schema)
        part = table.slice(0, g).combine_chunks()
        rest = table.slice(g)
        self.pending = rest.to_batches() if rest.num_rows else []
        self.pending_rows = rest.num_rows
        self.pending_bytes = sum(b.nbytes for b in self.pending)
        del table, rest
        self.backend.write_group(self.writer, part, g)

    def finish(self) -> None:
        """Decide (when fewer rows than the buffer arrived), write the rest, close the writer."""
        if self.mode == "types":
            return
        if not self.decided:
            self._decide_from_buffer()
        if self.pending_rows:
            self._write_group(self.pending_rows)
        check(self.ctx.cancel)
        writer, self.writer = self.writer, None
        if writer is not None:
            writer.close()

    def abort(self) -> None:
        writer, self.writer = self.writer, None
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - the file is removed anyway
                pass


# ----------------------------------------------------------------------------------
# the export
# ----------------------------------------------------------------------------------
def _type_rows(ctx: _Ctx, width: int) -> int:
    return max(TYPE_MIN_ROWS, min(TYPE_ROWS, TYPE_CELL_FACTOR * ctx.cell_budget // max(1, width)))


def _feed(ctx: _Ctx, builder: _RowBuilder, out: _Collector) -> None:
    """The rows of the selection into ``out``: worker processes (the CSV rule) or this thread."""
    workers = _pool_workers(ctx)
    if workers > 1:
        _RowsPool(ctx, builder, out, workers).run()
        return
    batches: Iterator[list[list[Any]]] = _iter_row_batches(ctx, builder)
    try:
        for batch in batches:
            out.add(batch)
            ctx.rows += len(batch)
    finally:
        batches.close()


class _State:
    """The counters and warnings of ``ctx`` before a pass (restored when the pass is redone)."""

    def __init__(self, ctx: _Ctx) -> None:
        self.records, self.rows, self.skipped = ctx.records, ctx.rows, ctx.skipped
        self.warnings = {k: dict(v) for k, v in ctx._warnings.items()}

    def restore(self, ctx: _Ctx) -> None:
        ctx.records, ctx.rows, ctx.skipped = self.records, self.rows, self.skipped
        ctx._warnings = {k: dict(v) for k, v in self.warnings.items()}


def _write(ctx: _Ctx, out: Path, pa: Any, backend: Any, names: list[str], cells: _ParquetCells,
           builder: _RowBuilder, type_rows: int, kinds: list[str] | None, group_rows: int | None,
           fmt: _Fmt) -> list[str]:
    """One writing pass (``kinds`` given: the fixed schema of the two-pass mode); returns the columns of
    numbers written as text. Raises :class:`_Restart`."""
    with atomic_path(out) as tmp:
        try:
            f = open(long_path(tmp), "wb", buffering=WRITE_BUFFER)  # noqa: SIM115
        except OSError as exc:
            raise os_error(exc, out) from exc
        coll: _Collector | None = None
        try:
            try:
                coll = _Collector(ctx, pa, backend, names, cells, "fixed" if kinds is not None else "single",
                                  type_rows, f, kinds, group_rows)
                _feed(ctx, builder, coll)
                coll.finish()
            except BaseException as exc:
                if coll is not None:
                    coll.abort()
                if isinstance(exc, pa.ArrowException):
                    raise ExportError(fmt.error_key, detail=str(exc)) from exc
                if isinstance(exc, _Restart) and kinds is not None:
                    raise ExportError(fmt.type_key, column=exc.column, index=exc.index) from None
                raise
            f.flush()
            os.fsync(f.fileno())
        finally:
            f.close()
        ctx.commit_check()
    assert coll is not None
    return coll.numbers_as_text


class _Fmt:
    """What differs between the Parquet and the Arrow IPC / Feather export: how the write backend is
    built (the optional pyarrow pieces and the compression) and the i18n keys that name the format."""

    def __init__(self, make_backend: Callable[[_Ctx], tuple[Any, Any]], *, error_key: str, type_key: str,
                 restarted_key: str, number_text_key: str, progress_types_key: str) -> None:
        self.make_backend = make_backend
        self.error_key = error_key
        self.type_key = type_key
        self.restarted_key = restarted_key
        self.number_text_key = number_text_key
        self.progress_types_key = progress_types_key


def _parquet_backend(ctx: _Ctx) -> tuple[Any, Any]:
    pa, pq = require_pyarrow()
    return pa, _ParquetBackend(pq, ctx.opts.parquet_compression)


def _feather_backend(ctx: _Ctx) -> tuple[Any, Any]:
    pa = require_pyarrow_ipc()
    codec = ctx.opts.feather_compression
    options = pa.ipc.IpcWriteOptions(compression=None if codec == "none" else codec)
    return pa, _ArrowBackend(pa, options)


_PARQUET = _Fmt(_parquet_backend, error_key="writer.error.parquet", type_key="writer.error.parquet_type",
                restarted_key="writer.warn.parquet_restarted", number_text_key="writer.warn.parquet_number_text",
                progress_types_key="writer.progress.parquet_types")
_FEATHER = _Fmt(_feather_backend, error_key="writer.error.feather", type_key="writer.error.feather_type",
                restarted_key="writer.warn.feather_restarted", number_text_key="writer.warn.feather_number_text",
                progress_types_key="writer.progress.feather_types")


def export_parquet(ctx: _Ctx, out: Path) -> list[Path]:
    """Write the rows of ``ctx`` (columns prepared by the writer) as the Parquet file ``out``."""
    return _export_columnar(ctx, out, _PARQUET)


def export_feather(ctx: _Ctx, out: Path) -> list[Path]:
    """Write the rows of ``ctx`` (columns prepared by the writer) as the Arrow IPC / Feather file ``out``."""
    return _export_columnar(ctx, out, _FEATHER)


def _export_columnar(ctx: _Ctx, out: Path, fmt: _Fmt) -> list[Path]:
    """The one-schema columnar export of ``ctx`` to ``out`` (module docstring); ``fmt``: the backend and
    the message keys."""
    pa, backend = fmt.make_backend(ctx)
    columns = list(ctx.columns)
    names = [_escape_surrogates(c.text) if _SURROGATE_RE.search(c.text) else c.text for c in columns]
    explicit = ctx.opts.columns is not None
    sep = ctx.opts.join_sep
    cells = _ParquetCells(sep)
    type_rows = _type_rows(ctx, len(columns))
    hidden = columns + [_Column(INDEX_COLUMN, special="index")]
    ctx.columns = hidden            # the rows jobs (worker processes) take the columns from ctx
    try:
        before = _State(ctx)
        builder = _RowBuilder(hidden, cells, ctx.explode, sep, track=explicit)
        try:
            as_text = _write(ctx, out, pa, backend, names, cells, builder, type_rows, None, None, fmt)
        except _Restart as restart:
            # the attempt is dropped (counters, warnings, temporary file); two passes follow
            before.restore(ctx)
            meter = ctx.meter
            meter.next_pass()
            if meter.progress is not None:
                meter.progress.set_total(meter.base + 2 * meter.unit)
            ctx.message(fmt.progress_types_key)
            builder = _RowBuilder(hidden, cells, ctx.explode, sep, track=explicit)
            types = _Collector(ctx, pa, backend, names, cells, "types", type_rows)
            _feed(ctx, builder, types)
            kinds, group_rows = types.decide_all()
            before.restore(ctx)
            ctx.warn(fmt.restarted_key, column=restart.column, index=restart.index, rows=type_rows)
            ctx.next_pass()
            ctx.message("writer.progress.export")
            builder = _RowBuilder(hidden, cells, ctx.explode, sep, track=explicit)
            _write(ctx, out, pa, backend, names, cells, builder, type_rows, kinds, group_rows, fmt)
            as_text = types.numbers_as_text
    finally:
        ctx.columns = columns
    if as_text:
        ctx.warn(fmt.number_text_key, add=len(as_text), columns=_names_text(as_text), names=as_text)
    _warn_unfound(ctx, builder)
    return [out]
