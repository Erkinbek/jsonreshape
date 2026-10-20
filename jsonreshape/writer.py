r"""Streaming exporters: JSON, JSON Lines, CSV, Excel (XLSX), SQLite and Parquet (docs/ARCHITECTURE.md §8).

Requirements: AGENT_PROMPT §10 "Konvertatsiya" (JSON <-> JSONL pretty or compact, indent
2 / 4 / tab, sorted keys, "Unicode \u ga aylantirilmaydi"; CSV and Excel with nested columns
``a.b.c`` and arrays joined with ``;`` or expanded into one row per element; xlsx split into
sheets or files above 1 048 576 rows; SQLite with one table per array and parent-child
links; export of a RecordSet), §6.4 (object keys sorted alphabetically), §1.7 (a source
changed by another program is detected), §12 (export speed, memory, number precision:
``0.1``, ``1e400``, ``12345678901234567890``, ``-0.0``, ``1.10`` never change and never
become strings) and §14 tests 22 and 24.

:func:`export` writes the records of a :class:`~jsonreshape.reader.Document` -- all of
them, or a ``source`` selection (a RecordSet-like object with ``len``/``[]``/
``iter_chunks()``, a ``range``, a list of record numbers or any iterable of them; the
order of the selection is kept) -- and returns an :class:`ExportReport`.
:func:`discover_columns` proposes the CSV/XLSX columns of a document.

Stage 5 (docs/ARCHITECTURE.md §14.4): :func:`export_many` writes the records of several
documents into one JSON / JSON Lines file (merge; see its docstring for the root and wrapper
rules); :func:`export_node` writes one node (JSON: re-indented; JSON Lines: one line per array
element; moved here from the export dialog so the CLI can use it); :func:`plan_columns` finds the
CSV/XLSX columns once for several exports (``export(..., column_plan=...)``: the parts of a split
share one header); a name ending with ``.gz`` (``.json.gz``, ``.jsonl.gz``, ``.csv.gz``) gives a
gzip-compressed JSON / JSON Lines / CSV output (``format_for_path`` knows these names;
``export(..., gzip=)`` decides explicitly; XLSX / SQLite cannot be compressed:
``writer.error.gzip_format``); the XLSX ``files`` parts are committed with
:func:`jsonreshape.fileio.commit_files` (POSIX-semantics renames).

Common behaviour
================
* The result is written to a temporary file in the target directory and renamed over the
  target only after everything succeeded (:func:`jsonreshape.fileio.atomic_write` /
  :func:`~jsonreshape.fileio.atomic_path`). On an error or
  :class:`~jsonreshape.errors.Cancelled` the temporary files are removed and an existing
  target stays untouched. The source file is never a valid target
  (``writer.error.same_file``: "Asl fayl hech qachon o'zgarmaydi").
* A target that has unsaved edits in this program -- an edit layer, live (a tab or another
  instance edits that file) or pending (kept for recovery) -- is never replaced: replacing it
  would leave edits that can never be saved. :func:`export`, :func:`export_node` and
  :func:`export_many` (and the XLSX ``files`` parts) refuse it with
  ``filetools.error.part_open_edited`` -- the rule and message of a split part (fix round R5,
  R5F-3) -- before any work and again right before the commit (fix round L1;
  :func:`edited_targets` tells such files; a file shown in a tab without edits is replaced).
* Right before the result is committed (renamed over the target) the source is checked
  with :meth:`Document.ensure_unchanged`: when another program changed or removed it
  during the export, :class:`~jsonreshape.errors.FileChangedError` is raised, the
  temporary output is removed and an existing target stays untouched.
* The index is built first when it is not complete (progress message
  ``writer.progress.index``). The export then reports progress in the unit of its total:
  bytes of record data read for all records or a ``range`` (exact), and for any other
  selection an estimate of the selection's bytes advanced in proportion to the records
  done -- ``done`` never exceeds ``total``. An export that scans the records first (CSV/XLSX
  with automatic columns, SQLite) counts both passes: the total is twice the estimate, the
  scan reports ``writer.progress.columns``, the writing pass ``writer.progress.export``.
  ``cancel`` is checked for every block and every 1024 records.
* Records are read with :meth:`Document.iter_blocks` (8 MiB blocks): memory stays bounded
  by one block plus one parsed record, whatever the file size; CSV/XLSX rows and SQLite
  rows are handed on in batches of at most ``Settings.memory_limit_bytes // 512`` cells
  (and 8192 rows), whatever the number of columns.
* Applications stage XLSX row data in ``xlsx-<pid>-<hex>`` folders under
  :func:`jsonreshape.appdirs.work_dir` (``<cache_dir>/tmp`` when a cache folder is
  configured); every export first removes such folders of processes that no longer run
  (and pre-0.2 ``xlsx-xxxxxxxx`` folders older than a day).
* A document with a JSON error exports the records before the error (warning
  ``writer.warn.partial``); broken JSON Lines lines are skipped (``writer.warn.broken_lines``).
  Whether a partial export is acceptable is the caller's decision (the CLI requires
  ``--allow-partial``).
* A record nested so deeply that it cannot be decoded in memory (``codec.loads`` refuses
  it; the scanner accepts 100 000 levels) never aborts an export: see the formats below.
* :attr:`ExportReport.warnings` holds localized texts with counts (a count of 1 uses the
  key ``<key>.one`` when it exists: English "1 number ... was"; Uzbek keeps the noun
  after a number singular anyway); :attr:`ExportReport.warning_keys` the matching i18n
  keys (without ``.one``).

JSON and JSON Lines
===================
* JSON: records that are values (array, jsonl, stream, brace_list and single documents)
  are written as ``[`` records ``]``; object-member records (keyed documents, wrapped
  documents whose record container is an object) as ``{`` members ``}``. A wrapped document
  with ``keep_wrapper=True`` keeps the bytes around the record container
  (``Document.wrapper_spans()``: ``{"meta": ..., "data": [...], "tail": ...}`` keeps
  ``meta`` and ``tail``); ``keep_wrapper=False`` writes only the record container. A
  ``single`` document exported with exactly one record writes just that value.
* Everything -- wrapper prefix, brackets, records, separators, wrapper suffix -- goes
  through ONE ``core.Reformatter(indent)``, so the output is exactly
  ``codec.dumps(document, indent=...)`` of what was selected: tokens (strings, numbers,
  literals) are copied byte for byte, only whitespace changes. Records are not parsed;
  number tokens never change and non-ASCII text is never ``\u``-escaped. Consecutive
  records of comma-separated sources (array, keyed, wrapped, brace_list) are fed as whole
  blocks (their separators are ``,`` plus whitespace); other selections are joined with
  ``,`` record by record.
* ``sort_keys`` (``"alpha"`` -- also ``True`` -- or ``"codepoint"``) parses each record
  (``codec.loads``) and writes it again with the keys of every object, at every level,
  sorted: ``"alpha"`` alphabetically with :func:`jsonreshape.sortkeys.collation_key`
  (Uzbek collation, natural order ``item2 < item10``, case-insensitive: ``apple, Banan,
  item2, item10, olma, sabzi, Zebo, oʻrik, shaftoli, анор``), ``"codepoint"`` by code
  point (``codec.dumps(value, sort_keys=True)``: ``Banan, Zebo, apple, ...``). Numbers stay
  exact; string escapes are normalized by orjson (an escaped ASCII letter becomes the
  letter itself; never an escape for non-ASCII). The order of the records (and of the
  members of a keyed document) is kept; the wrapper is not re-sorted. A record that cannot
  be decoded in memory (nested too deeply) is written unsorted (``writer.warn.deep_unsorted``).
* JSON Lines: one compact line per record, ``\n`` line ends (also after the last line).
  JSON Lines has no place for a wrapper or for the object holding keyed records: member
  records become ``{"key":value}`` lines and a wrapped document's wrapper is dropped
  (``writer.note.wrapper_not_written`` says so; also for CSV and XLSX, unless
  ``keep_wrapper=False`` asked for the records alone). The notice is given whether the record
  path was detected or given by hand (``--record-path``; the GUI's record-path choice reopens
  the document with it too): the wrapper is part of the document either way -- a JSON export
  keeps it -- and only ``keep_wrapper=False`` (``--no-wrapper``, ``extract``) says the records
  alone are wanted.
  Converting such lines back gives an array of one-member objects; ``as_keyed`` (below)
  rebuilds the keyed object from them -- the JSON export of the original document keeps the
  structure.
* Huge records (larger than ``Settings.large_node_bytes``) and the wrapper parts are
  streamed from the file in 1 MiB pieces (the Reformatter is always fed at most 1 MiB at a
  time, which also bounds the memory of the pure-Python core); with ``sort_keys`` a huge
  record is written unsorted (``writer.warn.huge_unsorted``).

Keyed object from one-member records (``as_keyed``, JSON only)
==============================================================
* The JSON Lines form of a keyed document (``{"id1": {...}}`` per line, see above) becomes
  the keyed object again: ONE object whose members are the members of the records, in
  record (selection) order -- ``{"id1": {...}, "id2": {...}}``. Every record must be an
  object with exactly one member. Its member is written token for token (keys and values
  never re-encoded: exact numbers, no ``\u`` escaping) through the same single
  ``core.Reformatter`` as every JSON export, so ``indent`` and ``sort_keys`` (the keys
  inside the values; the member keys keep their order) give exactly the bytes of the JSON
  export of the keyed document itself; no records give ``{}``.
* Any other record -- an array, a string, a number, ``true``/``false``/``null``, ``{}``, an
  object with two or more members (``{"a": 1, "a": 2}`` included) -- raises
  :class:`~jsonreshape.errors.ExportError` ``writer.error.not_single_member`` (params
  ``index`` = the 0-based record number, ``what`` = a localized description such as
  "massiv" / "2 a'zoli obyekt", ``kind``, ``members``) for the first such record of the
  selection; the atomic write saves nothing (an existing target is unchanged).
* The check costs one extra core-scanner pass (no parsing): per block the records are
  joined without their braces, ``{`` + ``m0,m1,...`` + ``}``, and one ``core.Scanner``
  (``MODE_CONTAINER``) counts the members of that object. The text is a valid object with as
  many members as records only when every record has exactly one member (records come from
  the validated index; a non-object or empty record makes it invalid or short of members,
  a record with n >= 1 members adds exactly n), so only a failing block is examined record
  by record to name the first offender. Huge records are streamed (counted by a scanner
  while they are written without their braces).
* Member keys repeated across records are written as they are (JSON allows them; the round
  trip stays faithful) and counted in ``writer.warn.duplicate_keys`` (first record and key
  named), compared exactly after decoding escapes while at most ``DUP_KEYS_MAX`` = 1 000 000
  keys taking at most ``memory_limit_bytes // 4`` bytes (estimated) are remembered; the
  keys after that are not checked (``writer.note.duplicate_keys_limited`` in the warning
  says how many were; no warning when no repeated key was found among them).
* The output root is always the keyed object: ``keep_wrapper`` has no effect (a wrapped
  document's wrapper is dropped). Documents whose records already are object members
  (keyed; wrapped with an object record container) are written as their normal JSON
  export -- identical bytes for a keyed document -- without a wrapper; no record check, no
  duplicate-key count.

CSV and XLSX
============
* Columns: ``opts.columns`` (path patterns of :mod:`jsonreshape.paths`, plus the special
  columns ``$key`` -- the member key of keyed records, empty otherwise -- ``$index`` -- the
  0-based record number -- and ``$`` -- the record value itself) or automatic columns
  (:func:`discover_columns`): the leaf paths of EVERY selected record, found by a scan
  before the export, in first-seen order -- so every value of every exported record lands
  in a column. Objects are descended; in join mode an array is one column at its path; in
  rows mode the elements of the expanded array are descended instead
  (``orders[*].oid``); member records get a leading ``$key`` column. An empty object is a
  column only when no other column lies above or below its path. The scan parses records
  with orjson and compares the keys and value types of each object with those already
  seen, so records shaped like earlier ones cost little more than parsing.
* Column explosion is bounded:

  - maps: the objects at a path (the records themselves included) with more than
    ``MAP_MIN_KEYS`` = 100 different keys and more than ``MAP_SPREAD`` = 20 times the keys
    of the widest of them, or more than ``MAP_MAX_KEYS`` = 2000 different keys, are used as
    maps (ids, dates, user names as keys). Their keys are counted: a key found in at least
    ``MAP_KEY_SHARE`` = 1 % of the scanned records keeps its column(s) -- at most the
    ``MAP_KEEP_MAX`` = 1000 most frequent keys of one map; a map by spread also needs a
    key in at least ``MAP_KEY_MIN`` = 2 records --, so the fields of a sparse table (a few
    of many optional fields per record) and fields such as ``id``/``name`` found in every
    record always keep their columns. Only the long tail of rare keys is pooled: ONE
    column named ``<path>.*`` (``*`` for the records; a real key ``*`` is written
    ``["*"]``, so the name is unambiguous) holds, as a compact JSON object, the members of that
    object whose keys have no column of their own (empty when there are none); when no
    key of a map keeps a column, the pooled column is the object itself (``scores``:
    the whole map as compact JSON). ``writer.warn.map_columns`` names the pooled columns
    and the number of pooled keys (``~n``: estimated beyond ``MAP_SKETCH`` = 1024).
    Keys are counted exactly until a path turns out to be a map, then with lossy
    counting in buckets of ``MAP_BUCKET`` = 1000 records (memory bounded for any number
    of keys); the scan walks only the keys it saw before, and stops walking those that
    became rare: a kept key the scan did not walk (to the end) is one column holding its
    whole value (objects as compact JSON), so no value is lost. As an explicit column,
    ``attrs.*`` keeps its wildcard meaning (the values of every member, joined);
  - objects nested deeper than ``MAX_DEPTH`` = 64 levels are one compact-JSON column
    (``writer.warn.deep_columns``);
  - more than ``MAX_AUTO_COLUMNS`` = 1000 columns: the objects below the deepest level
    that keeps the columns within the limit become compact-JSON columns
    (``writer.warn.columns_capped``); the top level is never collapsed.

  The scan itself tracks at most ``SCAN_MAX_PATHS`` paths (deeper objects are collapsed
  beyond that), so its memory is bounded too.
* Cells: strings as they are; numbers as their exact JSON text in CSV; ``true``/``false``;
  null and missing values empty; an array at a concrete column path: scalar elements joined
  with ``join_sep``, container elements as compact JSON; an object at a column path:
  compact JSON (``codec.dumps``); a wildcard column (``orders[*].oid``, ``*``, ``**``):
  all its values joined like an array.
* Rows mode (``array_mode="rows"``, implied by ``explode``): one row per element of the
  expanded array (``explode``, else the array before the first ``[*]`` of a column, else
  the first column whose values are arrays -- explicit columns are scanned until one is
  found). Columns at that path or below its ``[*]`` take the element's values (``orders``
  -> the element, ``orders[*].oid`` -> ``oid`` of the element); the other columns repeat.
  A missing or empty array -- or a value that is not an array -- gives one row with the
  columns evaluated on the record; automatic columns then keep a plain column at the
  expanded path when it ever held a value that is not an array (other than null), so that
  value is written too -- that column is filled only for records whose value there is not
  an array (the elements have their own columns).
* Explicit columns that find a value (null included) in no exported record are named in
  ``writer.warn.columns_not_found`` (a misspelled path?).
* Records that cannot be decoded in memory (nested too deeply): with explicit columns
  their values come from :func:`jsonreshape.stream.stream_values`; with automatic columns
  they are skipped (``writer.warn.deep_skipped``).
* CSV: :mod:`csv` writer, ``csv_delimiter``, minimal quoting, ``\r\n`` line ends, UTF-8 with
  a BOM when ``csv_bom`` (Excel); a lone surrogate (from a ``\ud800`` escape) is written as
  the text ``\ud800``.
* CSV rows in worker processes (stage-6 performance work, P6; like ``engine.apply``,
  ``bulk.preview`` and the JSON Patch export, docs/ARCHITECTURE.md §12.12, §13.2):
  ``export(..., workers=None)`` -- automatic: a selection of at least ``bulk.POOL_MIN_BYTES``
  (64 MiB) gets ``bulk.auto_workers`` spawned processes (4 with the default memory limit) when
  ``bulk._spawn_safe()``; ``workers=1`` keeps everything in the calling thread, ``n > 1`` asks
  for n. The workers receive the columns once and jobs of :data:`POOL_BLOCK_BYTES` of record
  bytes (at most ``2 * workers`` in flight); a job returns the block's rows as encoded CSV text
  (:class:`_RowsJob`, the same per-record code as the one-thread writer: decode, row, ``csv``
  module, ``utf-8``/``backslashreplace``) and the explicit columns it found. The calling thread
  reads the blocks and writes the results in block order; huge records, records that cannot be
  decoded in memory and records with more than :data:`POOL_SAFE_BRACKETS` opening brackets
  (whose decoding depends on the stack of the process) are handled there exactly as without
  workers, so the file is byte for byte the one-thread file (``csv`` writes every row on its
  own). XLSX: at most :data:`XLSX_POOL_WORKERS` workers prepare the typed cells the same way
  and the calling thread writes them with xlsxwriter in their order (xlsxwriter is the bound;
  the workbook is the same). A job stops once its text exceeds :data:`POOL_JOB_TEXT` characters
  (XLSX: :data:`POOL_JOB_CELLS` cells; rows mode can multiply a record); the rest of its block
  is the next job. A job a worker cannot do is done in the calling thread (its error, if any,
  is raised as without workers); ``BrokenProcessPool`` -- the workers cannot run -- moves the
  rest of the export to the calling thread. ``cancel`` is checked every :data:`POOL_WAIT`
  seconds while a result is awaited; the workers are stopped on any error. A ``.csv.gz``
  output is byte for byte the same too (:class:`_Sink` feeds zlib fixed pieces of the plain
  stream). :attr:`ExportReport.workers`: the worker processes used (1 = the calling thread).
* The column scan of automatic CSV/XLSX columns (and :func:`discover_columns` /
  :func:`plan_columns` of every record) runs in worker processes too, by the same rule
  (``discover_columns(..., workers=None)``): every job scans its block with its own
  :class:`_ColumnScan` and the calling thread merges the results in order
  (:class:`_ScanMerge`). The merge is exact -- the same columns, in the same order, as one
  scan -- unless an object path may become a map, the scan would track more than
  ``SCAN_MAX_PATHS`` paths, or rows mode infers the expanded array: then the scan is done
  (again) in the calling thread (its progress resumes where the workers' stopped).
* XLSX: ``xlsxwriter`` in ``constant_memory`` mode (rows streamed to disk) with
  ``strings_to_numbers``/``strings_to_formulas``/``strings_to_urls`` disabled; a bold,
  frozen header row on every sheet. Numbers are real numbers when they have at most 15
  significant digits and a decimal exponent within +-307 (Excel's precision and range;
  ``1.10`` is the number 1.1); others (``12345678901234567890``, ``1e400``,
  ``3.141592653589793``) are written as their exact text (``writer.warn.xlsx_number_text``).
  Booleans are Excel booleans. Texts longer than 32767 UTF-16 units are truncated
  (``writer.warn.xlsx_truncated``). When the rows (header included) exceed
  ``xlsx_max_rows`` they continue on new sheets ``Sheet1``, ``Sheet2``, ... (``xlsx_split=
  "sheets"``) or in new files (``"files"``): a result that needs more than one file is
  written as ``<stem>_1.xlsx``, ``<stem>_2.xlsx``, ... (a single file keeps the requested
  name). Every sheet/file starts with the header row. More than 16384 columns is an error.
* XLSX workbooks are written through a file object opened by the writer (long paths work;
  the handle is always closed, so the temporary file is removed after any error); it
  checks ``cancel`` for every piece the zip writer produces, so finishing the workbook (the
  zip assembly, the slowest part of a large export) can be cancelled too, and reports it
  as progress (``writer.progress.finish``, bytes written, total unknown). ``cancel`` is
  checked again before the result is committed.
* XLSX ``files`` split: the parts are staged next to the target and committed all or
  nothing -- existing targets are renamed aside first; when one cannot be replaced (open in
  Excel) nothing is changed (``writer.error.xlsx_part_locked``). Older ``<stem>_<k>.xlsx``
  parts beyond the new count are left alone and named in ``writer.warn.xlsx_stale_parts``.
* Huge records: with explicit ``columns`` their values come from
  :func:`jsonreshape.stream.stream_values` (rows mode expands the array through the
  on-disk :class:`~jsonreshape.reader.Node` index), capturing at most
  ``min(large_node_bytes, memory_limit_bytes // 4)`` bytes of values (at least 1 MiB,
  estimated Python objects). A record (or element) whose values need more is never
  dropped: its cells are then built as text straight from the ijson events
  (:func:`_stream_cells`, the same texts as above; a single scalar keeps its XLSX type),
  each holding at most a text cap measured in characters of the cell -- XLSX: Excel's
  32767; CSV: the capture budget shared by the path columns read together, at least
  ``CELL_TEXT_MIN`` = 1 Mi characters. A longer cell is cut at the cap
  (``writer.warn.cell_truncated`` names the first record and column); the other cells of
  the row are complete. With automatic columns huge records are skipped
  (``writer.warn.huge_skipped``).

Parquet
=======
``fmt="parquet"`` (``.parquet``; stage 7) is the third table format: the columns, rows and cell
texts of CSV/XLSX (``columns``, automatic columns, ``array_mode``, ``explode``, ``join_sep``) with
typed columns -- ``int64`` / ``double`` (only numbers a double holds exactly) / ``bool`` /
``string`` (mixed columns and numbers no type keeps exactly, as their exact text) -- decided from
the first 65 536 rows and redone in two passes when a later value does not fit; row groups of
about 64 MiB; ``parquet_compression`` (``zstd`` default); rows in worker processes exactly as
CSV; the optional ``pyarrow`` package (``writer.error.parquet_missing`` names
``pip install "jsonreshape[parquet]"``). The whole description is the docstring of
:mod:`jsonreshape.writer_parquet`, which :func:`export` calls through ``_EXPORTERS``.

SQLite
======
Table ``records``: ``_id`` (INTEGER PRIMARY KEY = the record number), ``$key`` for member
records, and one column per scalar leaf of nested objects named by its dotted path
(``contact.address.city``). Every array path gets a child table named by its dotted path
without element markers (``orders``, ``orders.items``; an array directly inside an array
keeps the marker: ``matrix[*]``; arrays that are records themselves: ``value``) with
``_id`` INTEGER PRIMARY KEY (1, 2, ... per table), ``_parent`` (``_id`` of the parent row in
``records`` or in the parent array's table), ``_idx`` (0-based element index) and the
element's scalar leaves (scalar elements: column ``value``; a scalar record: column
``value`` of ``records``). Data columns are declared without a type, so SQLite stores each
value as bound -- numbers stay numbers, so SQL comparisons and ORDER BY work:

* INTEGER for integers in the 64-bit range (``-0`` is 0);
* REAL for every other number whose decimal value a double holds exactly as written:
  ``0.1``, ``1.10`` and ``5.50`` (1.1, 5.5), ``1E3``, ``-0.0``, ``3.141592653589793``;
* TEXT -- the exact token text -- only for numbers no SQLite number can hold
  (``1e400``, ``1e-400``, ``123456789.123456789`` and other values with more digits than a
  double keeps, integers beyond 64 bits), counted in ``writer.warn.sqlite_number_text``;
* TEXT for strings, 0/1 for booleans, NULL for null.

The export makes two passes: a first one finds the maps (the rule of "CSV and XLSX",
applied to the objects at every path -- array elements included, ``orders[*].attrs``):
the rare keys of a map are stored together as one compact JSON object in the column
``<path>.*`` of the row (``*`` in ``records``), or, when no key of the map is kept, the
whole object is one JSON text in the column ``<path>`` (``value`` for a record); arrays
under pooled keys are inside that text, so map-like data never creates a table per key
and the time grows linearly (``writer.warn.map_columns``). Columns are added on the fly
(``ALTER TABLE ADD COLUMN``). A table never gets more than ``SQLITE_MAX_COLUMNS`` = 1500
columns (SQLite allows 2000): the leaves of a row that find no column are stored as one
compact JSON object ``{"path": value, ...}`` in the overflow column ``_extra`` (``_extra_2``
when the data has a field ``_extra``; ``writer.warn.sqlite_extra`` names it), so wide data
never aborts the export. At most ``SQLITE_MAX_TABLES`` = 200 child tables are created:
an array at a further path is stored as compact JSON text in the column of its path in the
parent row (``writer.warn.sqlite_tables``). Values nested deeper than ``MAX_DEPTH`` = 64
levels (objects and arrays) are stored as compact JSON text (``writer.warn.sqlite_deep``)
instead of further columns and tables. Rows are inserted in batches inside a transaction;
an index is created on every ``_parent``. Names are unique case-insensitively (a clash
gets ``_2``, ``_3``, ...).
Relations are described for SQL tools and DB browsers: every ``_parent`` is declared
``REFERENCES "<parent table>"("_id")`` (``PRAGMA foreign_keys`` stays off: no enforcement
cost), and the table ``_jsonreshape`` (``name``, ``path``, ``parent``, ``kind``, ``rows``) lists
every data table with the JSON path of its rows (``$`` for ``records``, ``orders[*]``,
``orders[*].items[*]``, ``[*]`` for the elements of array records), its parent table, its kind
(``records`` / ``array``) and its row count -- the parent of a renamed table (``_2``) is never
guessed from a name. A selection in another order than the file's (a sort, a hand-made list)
gets the column ``_ord`` after ``_id`` in ``records`` (0, 1, 2, ... in the order written: ``ORDER
BY _ord`` gives the selection's order). A wrapped document's wrapper is kept in the table
``_wrapper`` (``path``: the record path; ``json``: the wrapper as compact JSON with ``null`` in
place of the record container) unless it is larger than ``Settings.large_node_bytes`` or the
document is partial (then ``writer.note.wrapper_not_written``). The names ``_jsonreshape`` and
``_wrapper`` are reserved: data tables of those names get ``_2``.
``columns``/``array_mode`` do not apply. Huge records are skipped
(``writer.warn.huge_skipped_sqlite``), so are records that cannot be decoded in memory
(``writer.warn.deep_skipped_sqlite``); a record selected twice is written once
(``writer.warn.duplicates``).

Interpretations (where docs/ARCHITECTURE.md §8 leaves room)
===========================================================
* ``explode`` implies ``array_mode="rows"``; it must be a concrete path (no wildcards).
* XLSX ``files`` split names the parts ``<stem>_1.xlsx`` ... only when there is more than
  one part (the number of rows is not known in advance, so parts are staged and named at
  the end); an existing ``<stem>.xlsx`` is then left as it is.
* ``ExportReport.rows``: JSON/JSONL = records; CSV/XLSX = data rows (headers excluded);
  SQLite = rows inserted into all tables.
* Huge records read through ijson (``stream_values``) follow its documented deviations:
  ``-0`` arrives as ``0``; exponents may be normalized (``1e400`` -> ``1E+400``). Cells built
  from the stream (values beyond the capture budget) do not merge duplicate keys: a
  concrete column takes the last match, a wildcard column every match.
* :func:`discover_columns` without ``sample`` scans every selected record (as an export
  does); ``sample=N`` proposes columns from an evenly spread sample of N records instead
  (quick previews).
"""

from __future__ import annotations

import codecs
import contextlib
import csv
import heapq
import inspect
import io
import itertools
import logging
import math
import operator
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
import zlib
from array import array
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator, Sequence, Union

import orjson

from . import codec, core
from . import stream as _stream
from .appdirs import tmp_dir, work_dir
from .codec import JsonFloat, JsonInt
from .core import FLAG_ESCAPES, FLAG_U_ESCAPES, MODE_CONTAINER
from .errors import (
    Cancelled,
    EditError,
    ExportError,
    JsonDecodeError,
    JsonReshapeError,
    PathSyntaxError,
    UserInputError,
)
from .fileio import (
    FileIdentity,
    atomic_path,
    atomic_write,
    commit_files,
    long_path,
    move_replace,
    normalize_path,
    open_shared,
    os_error,
    read_range,
)
from .i18n import has, t
from .paths import MISSING, PathPattern, Wild, canonical_index, format_path, get, parse_path, parse_pattern
from .settings import XLSX_MAX_ROWS, Settings, get_settings
from .sortkeys import collation_key
from .stream import iter_events, stream_values
from .tasks import CancelToken, Progress, check, format_bytes

if TYPE_CHECKING:  # pragma: no cover
    from .reader import Block, Document

log = logging.getLogger(__name__)

StrPath = Union[str, "os.PathLike[str]"]

__all__ = [
    "ARRAY_MODES",
    "FORMATS",
    "GZIP_FORMATS",
    "MAX_AUTO_COLUMNS",
    "META_TABLE",
    "NODE_FORMATS",
    "PARQUET_COMPRESSIONS",
    "SORT_KEYS",
    "TABLE_FORMATS",
    "WRAPPER_TABLE",
    "XLSX_MAX_COLUMNS",
    "XLSX_MAX_STRING",
    "XLSX_SPLITS",
    "ColumnPlan",
    "FORMAT_NAMES",
    "ExportOptions",
    "ExportReport",
    "discover_columns",
    "edited_targets",
    "export",
    "export_many",
    "export_node",
    "format_for_path",
    "gzip_path",
    "plan_columns",
    "refuse_edited_targets",
]

KiB = 1 << 10
MiB = 1 << 20

FORMATS = ("json", "jsonl", "csv", "xlsx", "sqlite", "parquet")
GZIP_FORMATS = ("json", "jsonl", "csv")   # formats written gzip-compressed to a ".gz" name
TABLE_FORMATS = ("csv", "xlsx", "parquet")   # the formats with columns (``columns``, ``array_mode``, ``explode``)
FORMAT_NAMES = {"json": "JSON", "jsonl": "JSON Lines", "csv": "CSV", "xlsx": "Excel (XLSX)", "sqlite": "SQLite",
                "parquet": "Parquet"}
PARQUET_COMPRESSIONS = ("zstd", "snappy", "gzip", "none")   # ExportOptions.parquet_compression (writer_parquet)
GZIP_LEVEL = 6                    # zlib level of ".gz" outputs (one member, header mtime 0)
GZIP_FEED = 1 << 20               # ... fed to zlib in pieces of exactly this many bytes (_Sink)
ARRAY_MODES = ("join", "rows")
XLSX_SPLITS = ("sheets", "files")
SORT_KEYS = ("alpha", "codepoint")
XLSX_MAX_COLUMNS = 16384          # Excel: columns per sheet
XLSX_MAX_STRING = 32767           # Excel: UTF-16 code units per cell
XLSX_DIGITS = 15                  # Excel keeps 15 significant digits
XLSX_MAX_EXPONENT = 307           # Excel numbers: about 1e-307 .. 9.99e307
KEY_COLUMN = "$key"
INDEX_COLUMN = "$index"
RECORD_COLUMN = "$"
VALUE_NAME = "value"              # SQLite: column of scalar elements / table of array records
EXTRA_NAME = "_extra"             # SQLite: compact JSON of the leaves of a row that got no column
META_TABLE = "_jsonreshape"       # SQLite: one row per data table (name, path, parent, kind, rows)
WRAPPER_TABLE = "_wrapper"        # SQLite: the wrapper of a wrapped document (record path, JSON text)

BLOCK_BYTES = 8 * MiB             # iter_blocks() block size
FEED_BYTES = 1 * MiB              # Reformatter input per feed() (bounds the pure-Python core's
                                  # token lists; as fast as bigger pieces with the Rust core)
CHUNK_BYTES = FEED_BYTES          # huge records / wrapper parts are streamed in pieces this big
SAMPLE_BLOCK_BYTES = 256 * KiB    # sampled records: small reads (never the gaps between them)
WRITE_BUFFER = 1 * MiB
CSV_FLUSH_CHARS = 4 * MiB
CSV_ROWS_PER_CALL = 1024       # rows per csv writerows() call: one C call holds the GIL (GUI responsiveness)
ROW_BATCH = 8192                  # CSV/XLSX rows handed to the writer at a time, at most ...
CELL_BUDGET_DIVISOR = 512         # ... and at most memory_limit_bytes // 512 cells (1 Mi by default)
MIN_CELL_BUDGET = 4096
CHECK_EVERY = 1024                # records between cancel checks
NODE_BATCH = 4096                 # children fetched at a time when expanding a huge array
SQLITE_BATCH = 4096               # rows per executemany() (fewer for wide tables: cell budget)
SQLITE_COMMIT_BYTES = 256 * MiB   # source bytes between commits
SQLITE_MAX_COLUMNS = 1500         # columns per SQLite table (SQLite's limit is 2000)
MAX_DEPTH = 64                    # deeper objects: one JSON column (CSV/XLSX) / JSON text (SQLite)
MAX_AUTO_COLUMNS = 1000           # automatic CSV/XLSX columns (deeper objects collapse beyond)
MAP_MIN_KEYS = 100                # an object path with more different keys than this ...
MAP_SPREAD = 20                   # ... and more than 20x the keys of its widest object is a map,
MAP_MAX_KEYS = 2000               # ... and so is one with more different keys than this
MAP_KEY_SHARE = 0.01              # a key of a map found in at least 1 % of its objects ...
MAP_KEY_MIN = 2                   # ... and in at least 2 of them keeps a column of its own,
MAP_KEEP_MAX = 1000               # ... at most this many keys per map (the most frequent ones)
MAP_BUCKET = 1000                 # lossy counting of map keys: objects per bucket
MAP_SKETCH = 1024                 # different pooled keys are counted exactly up to this many
POOL_SEG = Wild.ANY_KEY           # last segment of a pooled column: ``attrs.*`` (``*`` for records)
SQLITE_MAX_TABLES = 200           # SQLite child tables (arrays at further paths: JSON text)
SCAN_MAX_PATHS = 100_000          # paths tracked by the column scan (deeper objects collapse)
SCAN_SIG_BUDGET = 65536           # object shapes remembered by the column scan ...
SCAN_SIG_PER_NODE = 4096          # ... in total and per object path
STALE_LEGACY_AGE = 86400.0        # old-style xlsx-xxxxxxxx staging folders older than this go
CAPTURE_MIN_BYTES = 1 * MiB       # floor of the huge-record capture budget (as recordset's)
CELL_TEXT_MIN = 1 * MiB           # floor of the text cap of a cell built from a stream (characters)
STREAM_CHECK_EVENTS = 1 << 14     # ijson events between cancel checks when cells are streamed
DUP_KEYS_MAX = 1_000_000          # as_keyed: member keys remembered for the duplicate-key warning, at most ...
DUP_KEY_OVERHEAD = 96             # ... (estimated bytes per remembered key besides its text) ...
DUP_MEMORY_DIVISOR = 4            # ... and at most memory_limit_bytes // 4 bytes of them
POOL_BLOCK_BYTES = 1 * MiB        # worker processes: record bytes per job (2 MiB: the same speed, ~25 MB more)
POOL_JOB_TEXT = 8 * MiB           # ... a job stops once its CSV text has this many characters ...
POOL_JOB_CELLS = 1 << 20          # ... or its XLSX rows this many cells
POOL_JOB_ROWS = 1024              # ... rows built at a time by a job (its memory: a few MB per worker) ...
POOL_PIECE_CHARS = 256 * KiB      # ... and CSV text encoded once this many characters are buffered
XLSX_POOL_WORKERS = 2             # ... XLSX: at most this many prepare the cells (xlsxwriter is the bound)
POOL_WAIT = 0.05                  # ... seconds between cancellation checks while a result is awaited
POOL_SAFE_BRACKETS = 4096         # ... records with more "[" / "{" bytes are decoded by the calling thread
POOL_MAX_PROCESSES = 61           # ... at most (ProcessPoolExecutor's limit on Windows)

_COMMA_KINDS = frozenset({"array", "keyed", "wrapped", "brace_list"})
_EXT_FORMATS = {
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".jsonlines": "jsonl",
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".sqlite": "sqlite",
    ".sqlite3": "sqlite",
    ".db": "sqlite",
    ".parquet": "parquet",
}
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_EXACT_INT = 10**XLSX_DIGITS      # |int| below this is an exact Excel number
_XLSX_SAFE_CHARS = XLSX_MAX_STRING // 2   # at most this many code points always fit a cell
_SURROGATE_RE = re.compile("[" + chr(0xD800) + "-" + chr(0xDFFF) + "]")
_NAME_BAD_RE = re.compile("[\x00-\x1f\x7f" + chr(0xD800) + "-" + chr(0xDFFF) + "]")
_STAGING_RE = re.compile(r"xlsx-(\d+)-[0-9a-f]+\Z")
_LEGACY_STAGING_RE = re.compile(r"xlsx-[a-z0-9_]{8}\Z")
# what the column scan saw at a path: a non-null scalar, an array, an empty object, an object
# written as one JSON column (map-like or too deep), null
_LEAF, _ARRAY, _EMPTY, _JSON, _NULL = 1, 2, 4, 8, 16
_COLUMN_KINDS = _LEAF | _ARRAY | _JSON | _NULL
_PLAIN_KINDS = _LEAF | _EMPTY | _JSON   # values at an expanded path that need a plain column
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
# a record that codec.loads cannot decode in memory (nested too deeply)
_DECODE_ERRORS = (JsonDecodeError, RecursionError)


def _tn(key: str, /, **params: Any) -> str:
    """:func:`t` with the singular variant ``<key>.one`` when ``count`` is 1 and that key
    exists (English "1 record was ..." / "2 records were ..."; the Uzbek texts keep the
    noun after a number in the singular anyway)."""
    count = params.get("count")
    if type(count) is int and count == 1 and has(key + ".one"):
        return t(key + ".one", **params)
    return t(key, **params)


class _NeverRaised(Exception):
    """Stands in for ``stream.CaptureLimitExceeded`` when the stream module has none."""


def _capture_error() -> type[BaseException]:
    return getattr(_stream, "CaptureLimitExceeded", None) or _NeverRaised


try:
    _STREAM_MAX_BYTES = "max_bytes" in inspect.signature(stream_values).parameters
except (TypeError, ValueError):  # pragma: no cover
    _STREAM_MAX_BYTES = False


# ----------------------------------------------------------------------------------
# options
# ----------------------------------------------------------------------------------
def _invalid(name: str, value: Any, expected: str, **params: Any) -> UserInputError:
    return UserInputError("writer.invalid_option", name=name, value=repr(value), expected=t(expected, **params))


def _choice(name: str, value: Any, choices: tuple[str, ...], expected: str) -> str:
    if isinstance(value, str):
        v = value.strip().lower()
        if v in choices:
            return v
    raise _invalid(name, value, expected)


def _norm_indent(value: Any) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise _invalid("indent", value, "writer.expected.indent")
    if isinstance(value, int):
        if 0 <= value <= 16:
            return int(value)
        raise _invalid("indent", value, "writer.expected.indent")
    if isinstance(value, str):
        if value == "\t":
            return "\t"
        s = value.strip().lower()
        if s in ("tab", "\\t"):
            return "\t"
        if s in ("", "none", "compact"):
            return None
        if s.isascii() and s.isdigit() and int(s) <= 16:
            return int(s)
    raise _invalid("indent", value, "writer.expected.indent")


def _check_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise _invalid(name, value, "writer.expected.bool")
    return value


def _norm_sort_keys(value: Any) -> bool | str:
    """``False``, ``"alpha"`` (also for ``True``) or ``"codepoint"``."""
    if value is False:
        return False
    if value is True:
        return "alpha"
    return _choice("sort_keys", value, SORT_KEYS, "writer.expected.sort_keys")


@dataclass
class _Column:
    """A parsed CSV/XLSX column: ``special`` (``"key"``/``"index"``) or a path pattern.
    Automatic columns may also be the pooled column of a map (``keep``: the keys of the
    object at ``segments[:-1]`` that have columns of their own) or the plain column at
    the expanded array's path (``plain``: filled only when the value there is not an
    array)."""

    text: str
    special: str | None = None
    segments: tuple = ()
    pattern: PathPattern | None = None
    keep: frozenset | None = None
    plain: bool = False


def _parse_column(text: str) -> _Column:
    if text == KEY_COLUMN:
        return _Column(text, special="key")
    if text == INDEX_COLUMN:
        return _Column(text, special="index")
    try:
        pattern = parse_pattern(text)
    except PathSyntaxError as exc:
        raise UserInputError("writer.bad_column", column=text, detail=exc.message) from None
    return _Column(text, segments=pattern.segments, pattern=pattern)


def _path_column(path: tuple, keep: frozenset | None = None, plain: bool = False) -> _Column:
    text = _column_text(path)
    return _Column(text, segments=path, pattern=PathPattern(path, text), keep=keep, plain=plain)


def _norm_columns(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or not value:
        raise _invalid("columns", value, "writer.expected.columns")
    out = []
    for col in value:
        if not isinstance(col, str) or not col.strip():
            raise _invalid("columns", value, "writer.expected.columns")
        text = col.strip()
        _parse_column(text)  # validate
        out.append(text)
    return out


def _norm_explode(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _invalid("explode", value, "writer.expected.explode")
    text = value.strip()
    try:
        parse_path(text)
    except PathSyntaxError as exc:
        raise UserInputError("writer.bad_explode", path=text, detail=exc.message) from None
    return text


def _norm_separator(name: str, value: Any) -> str:
    if isinstance(value, str) and 1 <= len(value) <= 16 and "\r" not in value and "\n" not in value:
        return value
    raise _invalid(name, value, "writer.expected.separator")


def _norm_delimiter(value: Any) -> str:
    if isinstance(value, str) and len(value) == 1 and value not in "\"\r\n":
        return value
    raise _invalid("csv_delimiter", value, "writer.expected.delimiter")


def _norm_max_rows(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 2 <= value <= XLSX_MAX_ROWS:
        return int(value)
    raise _invalid("xlsx_max_rows", value, "writer.expected.max_rows", min=2, max=XLSX_MAX_ROWS)


@dataclass
class ExportOptions:
    """What :func:`export` writes (docs/ARCHITECTURE.md §8).

    * ``fmt``: ``json``, ``jsonl``, ``csv``, ``xlsx``, ``sqlite`` or ``parquet`` (case-insensitive);
    * ``indent`` (JSON): ``None`` (compact), an int 0..16 (2, 4, ...) or ``"\\t"`` (``"tab"``
      is accepted); ``sort_keys`` (JSON/JSONL): ``False``, ``"alpha"`` (``True`` means the
      same: alphabetical, Uzbek collation, natural, case-insensitive) or ``"codepoint"``
      (code point order) -- the keys of every object of every record;
      ``keep_wrapper`` (JSON, wrapped documents): keep the bytes around the record container;
    * ``columns`` (CSV/XLSX/Parquet): column paths (``None`` = automatic, see :func:`discover_columns`);
      ``array_mode``: ``join`` or ``rows``; ``explode``: the array expanded into rows (a
      concrete path; implies ``rows``); ``join_sep``: separator of joined array elements;
    * ``csv_delimiter`` / ``csv_bom``; ``xlsx_max_rows`` (2..1048576, header row included)
      and ``xlsx_split`` (``sheets`` or ``files``); ``parquet_compression`` (``zstd`` -- the
      default --, ``snappy``, ``gzip`` or ``none``);
    * ``as_keyed`` (JSON only): write ONE object whose members are the members of the
      records -- every record must be a one-member object ``{"key": value}`` (the JSON Lines
      form of a keyed document), see "Keyed object from one-member records" in the module
      docstring; ``keep_wrapper`` has no effect then.

    Values are validated and normalized on creation and by :meth:`validate`
    (:class:`~jsonreshape.errors.UserInputError` ``writer.invalid_option`` /
    ``writer.bad_column`` / ``writer.bad_explode``); ``sort_keys=True`` becomes
    ``"alpha"``. :meth:`from_settings` takes the defaults from the user settings.
    """

    fmt: str
    indent: int | str | None = 2
    sort_keys: bool | str = False
    keep_wrapper: bool = True
    columns: list[str] | None = None
    array_mode: str = "join"
    explode: str | None = None
    join_sep: str = ";"
    csv_delimiter: str = ","
    csv_bom: bool = True
    xlsx_max_rows: int = XLSX_MAX_ROWS
    xlsx_split: str = "sheets"
    as_keyed: bool = False
    parquet_compression: str = "zstd"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Check and normalize every option (``UserInputError`` when invalid)."""
        self.fmt = _choice("fmt", self.fmt, FORMATS, "writer.expected.fmt")
        self.indent = _norm_indent(self.indent)
        self.sort_keys = _norm_sort_keys(self.sort_keys)
        self.keep_wrapper = _check_bool("keep_wrapper", self.keep_wrapper)
        self.columns = _norm_columns(self.columns)
        self.array_mode = _choice("array_mode", self.array_mode, ARRAY_MODES, "writer.expected.array_mode")
        self.explode = _norm_explode(self.explode)
        if self.explode is not None:
            self.array_mode = "rows"
        self.join_sep = _norm_separator("join_sep", self.join_sep)
        self.csv_delimiter = _norm_delimiter(self.csv_delimiter)
        self.csv_bom = _check_bool("csv_bom", self.csv_bom)
        self.xlsx_max_rows = _norm_max_rows(self.xlsx_max_rows)
        self.xlsx_split = _choice("xlsx_split", self.xlsx_split, XLSX_SPLITS, "writer.expected.split")
        self.parquet_compression = _choice("parquet_compression", self.parquet_compression, PARQUET_COMPRESSIONS,
                                           "writer.expected.parquet_compression")
        self.as_keyed = _check_bool("as_keyed", self.as_keyed)
        if self.as_keyed and self.fmt != "json":
            raise _invalid("as_keyed", self.as_keyed, "writer.expected.as_keyed", fmt=self.fmt)

    @classmethod
    def from_settings(cls, fmt: str, settings: Settings | None = None, **overrides: Any) -> ExportOptions:
        """Options for ``fmt`` with the export defaults of ``settings`` (default: the user
        settings: ``export_indent``, ``csv_bom``, ``csv_delimiter``, ``array_join_sep``,
        ``xlsx_max_rows``); ``overrides`` win."""
        s = settings or get_settings()
        values: dict[str, Any] = {
            "indent": s.export_indent,
            "csv_bom": s.csv_bom,
            "csv_delimiter": s.csv_delimiter,
            "join_sep": s.array_join_sep,
            "xlsx_max_rows": s.xlsx_max_rows,
        }
        values.update(overrides)
        return cls(fmt, **values)


def format_for_path(path: StrPath) -> str | None:
    """Export format implied by the extension of ``path`` (``.json``, ``.jsonl``/``.ndjson``,
    ``.csv``, ``.xlsx``, ``.sqlite``/``.sqlite3``/``.db``, ``.parquet``; ``.json.gz``, ``.jsonl.gz``,
    ``.csv.gz``, ... are the compressed JSON / JSON Lines / CSV outputs), or ``None``."""
    base, ext = os.path.splitext(os.fspath(path))
    ext = ext.lower()
    if ext == ".gz":
        fmt = _EXT_FORMATS.get(os.path.splitext(base)[1].lower())
        return fmt if fmt in GZIP_FORMATS else None
    return _EXT_FORMATS.get(ext)


def gzip_path(path: StrPath) -> bool:
    """``path`` names a gzip-compressed output (its name ends with ``.gz``)."""
    return os.fspath(path).lower().endswith(".gz")


@dataclass
class ExportReport:
    """Result of :func:`export`.

    ``records``: records written (records skipped by CSV/XLSX/SQLite -- huge ones, ones
    that cannot be decoded in memory -- are counted in ``skipped``); ``rows``: JSON/JSONL
    = records, CSV/XLSX = data rows (headers excluded), SQLite = rows of all tables;
    ``bytes_written``: total size of ``files``; ``warnings``: localized texts (with
    counts), ``warning_keys`` their i18n keys; ``elapsed``: seconds; ``columns``: CSV/XLSX
    column headers; ``tables``: SQLite tables (the data tables; the ``_jsonreshape`` /
    ``_wrapper`` description tables are not listed); ``per_input``: :func:`export_many` --
    the records written of each input, in input order; ``warning_params``: the parameters of
    each warning (parallel to ``warning_keys``; ``count`` included) -- what a caller that
    combines several reports needs to add counts up (the parts of a split); ``workers``: the
    worker processes that built the CSV rows or scanned for the CSV/XLSX columns (1 = the calling
    thread only; module docstring, "CSV and XLSX").
    """

    fmt: str
    records: int = 0
    rows: int = 0
    bytes_written: int = 0
    files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    skipped: int = 0
    columns: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    warning_keys: list[str] = field(default_factory=list)
    per_input: list[int] = field(default_factory=list)
    warning_params: list[dict[str, Any]] = field(default_factory=list)
    workers: int = 1

    @property
    def mb_per_s(self) -> float:
        """Output megabytes (10**6 bytes) per second."""
        return self.bytes_written / self.elapsed / 1e6 if self.elapsed > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "fmt": self.fmt,
            "records": self.records,
            "rows": self.rows,
            "bytes_written": self.bytes_written,
            "files": [str(p) for p in self.files],
            "warnings": list(self.warnings),
            "warning_keys": list(self.warning_keys),
            "elapsed": self.elapsed,
            "skipped": self.skipped,
            "columns": list(self.columns),
            "tables": list(self.tables),
            "per_input": list(self.per_input),
            "workers": self.workers,
        }


# ----------------------------------------------------------------------------------
# shared helpers
# ----------------------------------------------------------------------------------
def _pairs(data: bytes) -> array:
    a = array("Q")
    a.frombytes(data)
    return a


def _escape_surrogates(s: str) -> str:
    """Lone surrogates (only possible from ``\\ud800``-style escapes) -> their JSON escape
    text; they cannot be encoded as UTF-8."""
    return _SURROGATE_RE.sub(lambda m: "\\u%04x" % ord(m.group()), s)


def _ensure_index(doc: Document, progress: Progress | None, cancel: CancelToken | None) -> None:
    """Build the index when it is not complete (progress mapped onto ``progress``)."""
    if doc.index_complete:
        return
    sub = None
    if progress is not None:
        total = max(0, doc.data_size - doc.info.data_start)
        progress.set_message(t("writer.progress.index", name=doc.source_path.name))
        progress.set_total(total)
        sub = Progress(total, callback=lambda info: progress.update(info.done), min_interval=progress.min_interval)
    doc.build_index(progress=sub, cancel=cancel)


def _bad_record(index: Any, count: int) -> UserInputError:
    return UserInputError("writer.error.bad_record", index=index, count=count)


def _pin(doc: Document, source: Any) -> Document:
    """``doc.pinned()`` (docs/ARCHITECTURE.md §12.3: the whole export reads one overlay state;
    record numbers are its positions) and the stale-scope check: a ``source`` whose ``gen``
    belongs to another state raises ``EditError("edits.error.stale_scope")``."""
    pinned = getattr(doc, "pinned", None)
    if callable(pinned):
        doc = pinned()
    gen = getattr(source, "gen", None) if source is not None else None
    if gen is not None and gen != getattr(doc, "gen", 0):
        raise EditError("edits.error.stale_scope")
    return doc


def _normalize_source(doc: Document, source: Any) -> tuple[Any, int]:
    """``(source for iter_blocks, number of selected records)``. Lists, tuples and arrays
    are checked against the record count; other iterables are materialized into an
    ``array('Q')`` (a RecordSet-like object with ``len``/``[]`` is used as it is). The base
    set of the document -- of any view of the same live document (``RecordSet._is_base_of``)
    -- means "all records" of the state exported (``None``): its own length follows the live
    document, which may show another state than a pinned export."""
    total = doc.record_count
    if source is None:
        return None, total
    is_base = getattr(source, "_is_base_of", None)
    if callable(is_base) and is_base(doc):
        return None, total
    if isinstance(source, range):
        if len(source) and (min(source) < 0 or max(source) >= total):
            raise _bad_record(min(source) if min(source) < 0 else max(source), total)
        return source, len(source)
    if isinstance(source, (list, tuple)) or (isinstance(source, array) and source.typecode != "Q"):
        try:
            arr = array("Q", source)
        except (TypeError, OverflowError, ValueError):
            bad = next((i for i in source if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < total),
                       None)
            raise _bad_record(repr(bad), total) from None
        if arr and max(arr) >= total:
            raise _bad_record(max(arr), total)
        return arr, len(arr)
    if isinstance(source, array):
        if source and max(source) >= total:
            raise _bad_record(max(source), total)
        return source, len(source)
    if hasattr(source, "__len__") and hasattr(source, "__getitem__") \
            and not isinstance(source, (str, bytes, bytearray, dict)):
        return source, len(source)
    arr = array("Q")
    for i in source:
        try:
            k = operator.index(i)
        except TypeError:
            raise _bad_record(repr(i), total) from None
        if k < 0 or k >= total or isinstance(i, bool):
            raise _bad_record(k, total)
        arr.append(k)
    return arr, len(arr)


def _estimate_total(doc: Document, src: Any, count: int) -> int:
    """Expected bytes of record data read for the selection (exact for ``None`` and
    step-1 ranges, proportional otherwise; an edited document: from
    ``doc.effective_bytes()`` -- its first and last records may be edited, moved or new)."""
    if getattr(doc, "edited", False):
        full = max(0, int(doc.effective_bytes()))
        if src is None:
            return full
        total = doc.record_count
        return int(full * min(1.0, count / total)) if total > 0 and count > 0 else 0
    if src is None:
        return max(0, doc.data_size - doc.info.data_start)
    if count <= 0 or doc.record_count <= 0:
        return 0
    if isinstance(src, range) and src.step == 1:
        return doc.record_span(src.stop - 1)[1] - doc.record_span(src.start)[0]
    first = doc.record_span(0)[0]
    last = doc.record_span(doc.record_count - 1)[1]
    return int((last - first) * count / doc.record_count)


class _Meter:
    """Progress of one or more passes over a selection, always in the unit of the total
    (docs: "Common behaviour"): bytes read for all records or a step-1 range, otherwise
    the estimated bytes of the selection advanced in proportion to the records done.
    ``done`` never exceeds ``total``."""

    def __init__(self, progress: Progress | None, doc: Document, src: Any, count: int) -> None:
        self.progress = progress
        self.count = count
        self.by_records = src is not None and not (isinstance(src, range) and src.step == 1)
        self.unit = _estimate_total(doc, src, count)
        self.base = 0
        self.nbytes = 0
        self.nrecords = 0

    def start(self, passes: int) -> None:
        p = self.progress
        if p is not None:
            p.set_total(self.unit * passes)
            p.update(0)
            p.restart_clock()

    def advance(self, blk: Block) -> None:
        if self.by_records:
            self.nrecords += len(blk.indices)
            done = self.unit * self.nrecords // self.count if self.count else self.unit
        else:
            if blk.huge:
                s, e = blk.span(0)
                self.nbytes += e - s
            else:
                self.nbytes += len(blk.data)
            done = self.nbytes
        if self.progress is not None:
            self.progress.update(self.base + min(done, self.unit))

    def next_pass(self) -> None:
        self.base += self.unit
        self.nbytes = self.nrecords = 0
        if self.progress is not None:
            self.progress.update(self.base)


def _decode(raw: bytes, member: bool, split: Callable[[bytes], tuple[str, int]]) -> tuple[str | None, Any]:
    """``(member key or None, value)`` of a record (``codec.loads``)."""
    if member:
        key, vo = split(raw)
        return key, codec.loads(raw[vo:])
    return None, codec.loads(raw)


def _scan_loads(raw: bytes) -> Any:
    """A record value for the column scan: orjson when it can (number types do not matter
    there), else ``codec.loads`` (big numbers, lone surrogate escapes, deep nesting)."""
    try:
        return orjson.loads(raw)
    except orjson.JSONDecodeError:
        return codec.loads(raw)


# ----------------------------------------------------------------------------------
# column discovery: a scan of every record
# ----------------------------------------------------------------------------------
def _map_like(distinct: int, widest: int) -> bool:
    """An object path with these different keys (and at most ``widest`` keys in one
    object) is used as a map (module docstring, "CSV and XLSX")."""
    return distinct > MAP_MIN_KEYS and (distinct > MAP_MAX_KEYS or distinct > MAP_SPREAD * widest)


class _MapStats:
    """The keys of an object path used as a map: their exact counts until the path was
    recognized as a map, then lossy counting (Manku and Motwani) in buckets of
    :data:`MAP_BUCKET` records -- when a bucket is complete, the keys whose count, plus
    the occurrences it may have missed before the key was (re)inserted, is at most the
    number of complete buckets are forgotten (they are in fewer than 1 of MAP_BUCKET
    records, far below :data:`MAP_KEY_SHARE`), so the memory stays bounded for any number
    of keys. The different keys are counted with a K-minimum-values sketch (exact up to
    :data:`MAP_SKETCH` keys, an estimate beyond).

    The keys seen before the path was recognized as a map (``known``) are walked on by
    the scans (their values may have columns of their own) until, at the end of a bucket,
    they are found in fewer than :data:`MAP_KEY_SHARE` of the records so far: then they
    are ``stopped``, and a stopped key that is frequent in the end keeps one column for
    its whole value (like a key first seen later)."""

    __slots__ = ("counts", "deltas", "bucket", "known", "walk", "stopped", "force", "dropped", "widest", "_mins",
                 "_heap")

    def __init__(self, counts: dict[str, int], records: int, force: Any = None, widest: int = 0) -> None:
        self.counts: dict[str, int] = dict(counts)
        self.deltas: dict[str, int] = dict.fromkeys(self.counts, 0)  # occurrences a count may miss
        self.bucket = max(0, records - 1) // MAP_BUCKET     # complete buckets of records
        self.known = frozenset(self.counts)                 # keys seen before ...
        self.walk = self.known                              # ... that the scans still walk
        self.stopped: set[str] = set()                      # ... that they no longer walk
        self.force = force                                  # a key that always keeps its column
        self.dropped = False                                # some key was forgotten (it is pooled)
        self.widest = widest                                # most keys of one object
        self._mins: set[int] = set()
        self._heap: list[int] = []                          # the smallest key hashes, negated
        for k in self.counts:
            self._sketch(k)

    def add(self, keys: dict | tuple, records: int) -> None:
        """Count the keys of one more object of record number ``records`` (1-based)."""
        if len(keys) > self.widest:
            self.widest = len(keys)
        done = (records - 1) // MAP_BUCKET
        if done > self.bucket:
            counts, deltas = self.counts, self.deltas
            gone = [k for k, c in counts.items() if c + deltas[k] <= done and k != self.force]
            for k in gone:
                del counts[k]
                del deltas[k]
            if gone:
                self.dropped = True
            self.bucket = done
            need = max(MAP_KEY_MIN, math.ceil(MAP_KEY_SHARE * records))
            stop = [k for k in self.walk if counts.get(k, 0) + deltas.get(k, 0) < need and k != self.force]
            if stop:
                self.stopped.update(stop)
                self.walk = self.walk.difference(stop)
        counts = self.counts
        get_count = counts.get
        for k in keys:
            c = get_count(k)
            if c is None:
                counts[k] = 1
                self.deltas[k] = self.bucket
                self._sketch(k)
            else:
                counts[k] = c + 1

    def _sketch(self, key: str) -> None:
        h = zlib.crc32(key.encode("utf-8", "surrogatepass"))
        mins = self._mins
        if h in mins:
            return
        heap = self._heap
        if len(heap) < MAP_SKETCH:
            heapq.heappush(heap, -h)
            mins.add(h)
        elif h < -heap[0]:
            mins.discard(-heapq.heapreplace(heap, -h))
            mins.add(h)

    def kept(self, records: int) -> list[str]:
        """The keys with a column of their own, in the order they were first counted:
        found in at least :data:`MAP_KEY_SHARE` of the ``records`` scanned -- and at least
        :data:`MAP_KEY_MIN` times when the objects are maps by their spread (far more keys
        than one object has), not just very wide objects; a count's possible misses are
        given the benefit of the doubt --, at most the :data:`MAP_KEEP_MAX` most frequent
        ones; ``force`` always."""
        spread = self.distinct()[0] > MAP_SPREAD * self.widest
        need = max(MAP_KEY_MIN if spread else 1, math.ceil(MAP_KEY_SHARE * records))
        counts, deltas = self.counts, self.deltas
        keys = [k for k, c in counts.items() if c + deltas[k] >= need]
        if len(keys) > MAP_KEEP_MAX:
            top = set(sorted(keys, key=lambda k: -counts[k])[:MAP_KEEP_MAX])   # stable: earlier keys win ties
            keys = [k for k in keys if k in top]
        if self.force is not None and self.force in counts and self.force not in keys:
            keys.append(self.force)
        return keys

    def distinct(self) -> tuple[int, bool]:
        """``(different keys counted, exact)``."""
        n = len(self._heap)
        if n < MAP_SKETCH:
            return n, True
        return int((MAP_SKETCH - 1) * float(1 << 32) / (-self._heap[0] + 1)), False

    def pooled(self, kept: int) -> tuple[int, bool] | None:
        """``(number of pooled keys, exact)``, or ``None`` when every key keeps its column."""
        if not (self.dropped or len(self.counts) > kept):
            return None
        distinct, exact = self.distinct()
        return max(1, distinct - kept), exact


@dataclass
class _MapPlan:
    """What becomes of the keys of a map (an object path with :class:`_MapStats`)."""

    path: tuple
    kept: frozenset            # keys with a column (or SQLite columns/tables) of their own
    late: list                 # kept keys the scan did not walk (to the end): one column for the value
    whole: frozenset           # ... the same as a set
    pool: tuple | None         # the pooled column: ``path + (POOL_SEG,)``, or ``path`` when no key
                               # is kept; None when nothing is pooled
    pooled: tuple[int, bool] | None   # (pooled keys, exact) for the warning


def _map_plan(path: tuple, stats: _MapStats, records: int) -> _MapPlan:
    kept = stats.kept(records)
    pooled = stats.pooled(len(kept))
    pool = None if pooled is None else (path + (POOL_SEG,) if kept else path)
    late = [k for k in kept if k not in stats.walk]
    return _MapPlan(path, frozenset(kept), late, frozenset(late), pool, pooled)


def _pool_text(plan: _MapPlan) -> str:
    """``column (keys: n)`` of a pooled map for ``writer.warn.map_columns``."""
    assert plan.pool is not None and plan.pooled is not None
    n, exact = plan.pooled
    return t("writer.warn.map_columns.item", column=_column_text(plan.pool), keys=f"{n}" if exact else f"~{n}")


def _apply_maps(kinds: dict[tuple, int], plans: dict[tuple, _MapPlan]) -> dict[tuple, int]:
    """``kinds`` without the paths below the pooled keys of maps (and with the paths below
    kept keys the scan stopped walking replaced by the key's column); a map's marker
    (``path.*``, registered when the map was recognized: after the paths seen before)
    brings the columns of the kept keys the scan did not walk, then the pooled column."""
    if not plans:
        return kinds
    out: dict[tuple, int] = {}
    for p, k in kinds.items():
        q: tuple | None = p
        for i in range(len(p)):
            plan = plans.get(p[:i])
            if plan is None:
                continue
            seg = p[i]
            if seg is POOL_SEG and i == len(p) - 1:
                for key in plan.late:
                    lp = p[:i] + (key,)
                    out[lp] = out.get(lp, 0) | _JSON
                q, k = plan.pool, _JSON
                break
            if seg not in plan.kept:      # pooled: the column comes with the marker
                q = None
                break
            if i + 1 < len(p) and seg in plan.whole:    # a stopped key: one column for its value
                q, k = p[:i + 1], _JSON
                break
        else:
            if p and p[-1] is POOL_SEG:     # the marker of an object collapsed meanwhile
                q = None
        if q is not None:
            out[q] = out.get(q, 0) | k
    return out


class _Node:
    """An object path seen by the column scan."""

    __slots__ = ("path", "keys", "nslow", "widest", "first", "sigs", "kids", "collapsed", "exempt", "force", "map",
                 "kbits")

    def __init__(self, path: tuple, exempt: bool, force: Any = None) -> None:
        self.path = path
        self.keys: Counter[str] = Counter()      # keys of the objects walked by _slow
        self.nslow = 0                           # objects walked by _slow
        self.widest = 0                          # most keys of one object here
        self.first = 0                           # keys of the first non-empty object here (_ScanMerge)
        self.sigs: dict[tuple, list] = {}        # (keys, value types) -> [objects _fast saw, what to descend into]
        self.kids: dict[str, _Node] = {}         # nodes of the object-valued members
        self.collapsed = False                   # one JSON column (too deep, or the scan's path limit)
        self.exempt = exempt                     # on the way to the expanded array: never collapsed ...
        self.force = force                       # ... and the key on that way keeps its column
        self.map: _MapStats | None = None        # the objects here are maps ...
        self.kbits: dict[Any, int] = {}          # ... what their walked keys held (not objects;
                                                 # None: an empty map object was seen)


class _Space:
    """The paths of one kind of value: the records, or the elements of one array."""

    __slots__ = ("base", "kinds", "root")

    def __init__(self, base: tuple, root: _Node) -> None:
        self.base = base
        self.kinds: dict[tuple, int] = {}        # path -> what was seen there (first-seen order)
        self.root = root


def _cut(p: tuple, collapsed: dict[tuple, None], limit: int | None) -> tuple | None:
    """The column that holds the path ``p``: its shortest collapsed prefix, or its prefix
    of length ``limit``; ``None`` when ``p`` is a column of its own."""
    n = len(p)
    if collapsed:
        for i in range(n + 1):
            if p[:i] in collapsed:
                if limit is not None and i > limit:
                    return p[:limit]
                return p[:i]
    if limit is not None and n > limit:
        return p[:limit]
    return None


def _assemble(kinds: dict[tuple, int], collapsed: dict[tuple, None], limit: int | None) -> list[tuple]:
    """Column paths of one space in first-seen order: every leaf/array path, one column
    for each collapsed object (and, with ``limit``, each object at that depth) in place of
    the paths below it, plus empty-object paths that no other path lies above or below."""
    if collapsed or limit is not None:
        merged: dict[tuple, int] = {}
        for p, k in kinds.items():
            q = _cut(p, collapsed, limit)
            if q is not None:
                p, k = q, _JSON
            merged[p] = merged.get(p, 0) | k
        kinds = merged
    if all(k & _COLUMN_KINDS for k in kinds.values()):
        return list(kinds)
    real = {p for p, k in kinds.items() if k & _COLUMN_KINDS}
    below: set[tuple] = set()            # strict prefixes of any path
    for p in kinds:
        for i in range(len(p)):
            below.add(p[:i])
    out = []
    for p, k in kinds.items():
        if k & _COLUMN_KINDS or (p not in below and not any(p[:i] in real for i in range(len(p)))):
            out.append(p)
    return out


class _ColumnScan:
    """Leaf paths of every record handed to :meth:`add` (module docstring, "CSV and XLSX").

    For each object path the scan remembers the shapes (keys and value types) it has
    seen; a record whose objects all have known shapes adds nothing and is only checked
    and counted (:meth:`_fast`), any other record is walked completely (:meth:`_slow`,
    iterative, so the first-seen order of the paths is exactly that of a walk of every
    record). Every object is counted -- per shape by :meth:`_fast`, per key by
    :meth:`_slow` -- so the keys of a path recognized as a map have exact counts up to
    then (:class:`_MapStats` counts on). Rows mode also collects the paths of the elements
    of the expanded array (``explode``), or, when it is inferred, of the first array path
    in first-seen order."""

    def __init__(self, rows: bool, explode: tuple | None) -> None:
        self.explode = explode
        self.infer = rows and explode is None
        self.exempt = {explode[:i] for i in range(len(explode) + 1)} if explode is not None else set()
        self.main = _Space((), self._node(()))
        self.elements: _Space | None = None
        if explode is not None:
            base = explode + (Wild.ANY_INDEX,)
            self.elements = _Space(base, _Node(base, False))
        self.cand: tuple | None = None        # inferred rows mode: the first array path so far
        self.cand_pos = 0
        self.order: dict[tuple, int] = {}    # inferred rows mode: path -> first-seen position
        self.depth = MAX_DEPTH
        self.collapsed: dict[tuple, None] = {}
        self.map_nodes: list[_Node] = []
        self.deep: dict[tuple, None] = {}
        self.capped: int | None = None        # depth used to keep MAX_AUTO_COLUMNS (columns())
        self.tracked = 0
        self.budget = SCAN_SIG_BUDGET
        self.records = 0
        self.undecodable = 0
        self.workers = 1                      # worker processes that scanned (_ScanPool)
        self._plans: dict[tuple, _MapPlan] | None = None

    def _node(self, path: tuple) -> _Node:
        if path in self.exempt:
            assert self.explode is not None
            n = len(path)
            return _Node(path, True, self.explode[n] if n < len(self.explode) else None)
        return _Node(path, False)

    # -- records ---------------------------------------------------------------------------
    def add(self, value: Any) -> None:
        self.records += 1
        if type(value) is not dict or not self._fast(self.main.root, value):
            self._slow(self.main, value)
        if self.explode is not None:
            arr = get(value, self.explode)
            if type(arr) is list:
                assert self.elements is not None
                self._items(self.elements, arr)

    def _items(self, space: _Space, items: list) -> None:
        root, base, kinds = space.root, space.base, space.kinds
        for item in items:
            ti = type(item)
            if ti is dict:
                if not self._fast(root, item):
                    self._slow(space, item)
            else:
                bit = _ARRAY if ti is list else _NULL if item is None else _LEAF
                if not kinds.get(base, 0) & bit:
                    self._slow(space, item)

    def _fast(self, node: _Node, v: dict) -> bool:
        """True when every object of ``v`` has a shape already seen at its path (nothing
        new): the objects are then counted per shape (maps: per key, the walked keys must
        hold what they held before)."""
        stack: list[tuple[_Node, Any]] | None = None
        hit: list[list] = []
        later: list[Any] | None = None
        maps: list[tuple[_MapStats, dict]] | None = None
        while True:
            m = node.map
            if m is not None:
                kids = None if self.infer else self._fast_map(node, m, v)
                if kids is None:
                    break
                if kids:
                    if stack is None:
                        stack = kids
                    else:
                        stack.extend(kids)
                if maps is None:
                    maps = [(m, v)]
                else:
                    maps.append((m, v))
            elif not node.collapsed:
                e = node.sigs.get((tuple(v), tuple(map(type, v.values()))))
                if e is None:
                    break
                e[0] += 1
                hit.append(e)
                for key, kid, lpath in e[1]:
                    if kid is not None:
                        if stack is None:
                            stack = [(kid, v[key])]
                        else:
                            stack.append((kid, v[key]))
                    elif lpath == self.cand:
                        if later is None:
                            later = [v[key]]
                        else:
                            later.append(v[key])
            if not stack:
                if maps is not None:
                    records = self.records
                    for m, mv in maps:
                        m.add(mv, records)
                if later is not None:
                    assert self.elements is not None
                    for items in later:
                        self._items(self.elements, items)
                return True
            node, v = stack.pop()
        for e in hit:              # the record is walked by _slow instead: count it once
            e[0] -= 1
        return False

    @staticmethod
    def _fast_map(node: _Node, m: _MapStats, v: dict) -> list[tuple[_Node, Any]] | None:
        """The object-valued walked keys of a map object (``(kid, value)``), or ``None``
        when a walked key holds something new (:meth:`_slow` registers it)."""
        walk = m.walk
        kbits = node.kbits
        if not v:
            return [] if None in kbits else None      # an empty object: marked once by _slow
        kids = node.kids
        out: list[tuple[_Node, Any]] = []
        for key, child in v.items():
            if key in walk:
                tc = type(child)
                if tc is dict:
                    kid = kids.get(key)
                    if kid is None:
                        return None
                    out.append((kid, child))
                elif not kbits.get(key, 0) & (_ARRAY if tc is list else _NULL if child is None else _LEAF):
                    return None
        return out

    def _slow(self, space: _Space, value: Any) -> None:
        """Pre-order walk registering every new path / kind (and the object shapes),
        counting the keys of every object (maps: only the keys seen before the path was
        recognized as a map are walked)."""
        main = space is self.main
        lists = main and self.infer
        stack: list[tuple[tuple, Any, _Node | None]] = [(space.base, value, space.root)]
        while stack:
            if self.tracked > SCAN_MAX_PATHS:
                self._shrink()
            path, v, node = stack.pop()
            cap = max(self.depth, len(space.base))
            if len(path) > cap:          # below an object collapsed meanwhile (_shrink)
                q = path[:cap]
                self._mark(space, q, _JSON)
                if q not in self.collapsed:
                    self.collapsed[q] = None
                    self.deep[q] = None
                continue
            tv = type(v)
            if tv is dict:
                assert node is not None
                if node.collapsed:
                    self._mark(space, path, _JSON)
                    continue
                m = node.map
                if m is not None:
                    m.add(v, self.records)
                    if not v:
                        self._mark(space, path, _EMPTY)
                        node.kbits[None] = _EMPTY
                        continue
                    walk = m.walk
                    kids = node.kids
                    kbits = node.kbits
                    push: list[tuple[tuple, Any, _Node | None]] = []
                    for key, child in v.items():
                        if key in walk:
                            cpath = path + (key,)
                            tc = type(child)
                            if tc is dict:
                                kid = kids.get(key)
                                if kid is None:
                                    kid = kids[key] = self._node(cpath)
                                push.append((cpath, child, kid))
                            else:
                                kbits[key] = kbits.get(key, 0) | (_ARRAY if tc is list else
                                                                  _NULL if child is None else _LEAF)
                                push.append((cpath, child, None))
                    push.reverse()
                    stack.extend(push)
                    continue
                node.nslow += 1
                if not v:
                    self._mark(space, path, _EMPTY)
                    self._remember(node, (), (), ())
                    continue
                if len(path) >= cap and not node.exempt:
                    self._collapse(space, node)
                    continue
                keys = tuple(v)
                node.keys.update(keys)
                if len(keys) > node.widest:
                    if not node.widest:
                        node.first = len(keys)
                    node.widest = len(keys)
                if _map_like(len(node.keys), node.widest) and path != self.explode:
                    self._to_map(space, node)
                types = tuple(map(type, v.values()))
                desc = []
                push = []
                kids = node.kids
                for key, child, tc in zip(keys, v.values(), types):
                    cpath = path + (key,)
                    if tc is dict:
                        kid = kids.get(key)
                        if kid is None:
                            kid = kids[key] = self._node(cpath)
                        desc.append((key, kid, None))
                        push.append((cpath, child, kid))
                    else:
                        if tc is list and lists:
                            desc.append((key, None, cpath))
                        push.append((cpath, child, None))
                push.reverse()
                stack.extend(push)
                self._remember(node, keys, types, tuple(desc))
            elif tv is list:
                self._mark(space, path, _ARRAY)
                if lists:
                    self._array(path, v)
            else:
                self._mark(space, path, _NULL if v is None else _LEAF)

    # -- bookkeeping -----------------------------------------------------------------------
    def _mark(self, space: _Space, path: tuple, bit: int) -> None:
        kinds = space.kinds
        old = kinds.get(path)
        if old is None:
            kinds[path] = bit
            self.tracked += 1
            if space is self.main and self.infer:
                self.order[path] = len(self.order)
        elif not old & bit:
            kinds[path] = old | bit

    def _remember(self, node: _Node, keys: tuple, types: tuple, desc: tuple) -> None:
        if node.map is None and self.budget > 0 and len(node.sigs) < SCAN_SIG_PER_NODE:
            sig = (keys, types)
            if sig not in node.sigs:
                node.sigs[sig] = [0, desc]
                self.budget -= 1

    def _collapse(self, space: _Space, node: _Node) -> None:
        """An object nested too deeply: one JSON column."""
        node.collapsed = True
        node.keys = Counter()
        node.kids = {}
        node.sigs = {}
        node.map = None
        self._mark(space, node.path, _JSON)
        self.collapsed[node.path] = None
        self.deep[node.path] = None

    def _to_map(self, space: _Space, node: _Node) -> None:
        """The objects at ``node`` are maps: their key counts so far (per key from _slow,
        per shape from _fast) go to a :class:`_MapStats`; the marker ``path.*`` fixes the
        place of the pooled column in the first-seen order."""
        counts = node.keys
        for (keys, _types), e in node.sigs.items():
            n = e[0]
            if n:
                for k in keys:
                    counts[k] += n
        node.map = _MapStats(counts, self.records, node.force, node.widest)
        node.keys = Counter()
        node.sigs = {}
        self.map_nodes.append(node)
        self._mark(space, node.path + (POOL_SEG,), _JSON)

    def _array(self, path: tuple, items: list) -> None:
        """Inferred rows mode: an array at ``path`` of the records."""
        pos = self.order[path]
        if self.cand is None or pos < self.cand_pos:
            self.cand = path
            self.cand_pos = pos
            base = path + (Wild.ANY_INDEX,)
            self.elements = _Space(base, _Node(base, False))
        if path == self.cand:
            assert self.elements is not None
            self._items(self.elements, items)

    def _spaces(self) -> list[_Space]:
        return [self.main] if self.elements is None else [self.main, self.elements]

    def _shrink(self) -> None:
        """Too many different paths: collapse the deepest objects until half of
        ``SCAN_MAX_PATHS`` is left (bounded memory for any input)."""
        while self.tracked > SCAN_MAX_PATHS // 2 and self.depth > 1:
            deepest = max((len(p) for sp in self._spaces() for p in sp.kinds), default=1)
            self.depth = max(1, min(self.depth, deepest) - 1)
            total = 0
            for sp in self._spaces():
                cap = max(self.depth, len(sp.base))
                new: dict[tuple, int] = {}
                for p, k in sp.kinds.items():
                    if len(p) > cap:
                        q = p[:cap]
                        new[q] = new.get(q, 0) | _JSON
                        if q not in self.collapsed:
                            self.collapsed[q] = None
                            self.deep[q] = None
                    else:
                        new[p] = new.get(p, 0) | k
                sp.kinds = new
                total += len(new)
                stack = [sp.root]
                while stack:
                    node = stack.pop()
                    if len(node.path) >= cap and not node.exempt:
                        if not node.collapsed:
                            node.collapsed = True
                            node.keys, node.kids, node.sigs, node.map = Counter(), {}, {}, None
                    else:
                        stack.extend(node.kids.values())
            self.tracked = total
            if self.infer:
                self.order = {p: i for i, p in enumerate(self.main.kinds)}
                if self.cand is not None:
                    self.cand_pos = self.order.get(self.cand, self.cand_pos)

    # -- result ----------------------------------------------------------------------------
    def plans(self) -> dict[tuple, _MapPlan]:
        """The maps (object path -> :class:`_MapPlan`) of the records seen so far."""
        if self._plans is None:
            self._plans = {n.path: _map_plan(n.path, n.map, self.records) for n in self.map_nodes
                           if n.map is not None and not n.collapsed}
        return self._plans

    def _paths(self, limit: int | None) -> tuple[list[tuple], tuple | None]:
        plans = self.plans()
        kinds = _apply_maps(self.main.kinds, plans)
        paths = _assemble(kinds, self.collapsed, limit)
        if self.explode is None and not self.infer:
            return paths, None
        explode = self.explode
        if explode is None:
            explode = next((p for p in paths if kinds.get(p, 0) & _ARRAY), None)
            if explode is None:
                return paths, None
        sp = self.elements
        elem_paths: list[tuple] = []
        if sp is not None and sp.base[:-1] == explode:
            elem_paths = _assemble(_apply_maps(sp.kinds, plans), self.collapsed,
                                   None if limit is None else max(limit, len(sp.base)))
        try:
            i = paths.index(explode)
        except ValueError:
            return paths + elem_paths, explode
        plain = kinds.get(explode, 0) & _PLAIN_KINDS or not elem_paths
        return paths[:i] + ([explode] if plain else []) + elem_paths + paths[i + 1:], explode

    def columns(self) -> tuple[list[tuple], tuple | None]:
        """``(column paths, expanded array path or None)``; more than
        ``MAX_AUTO_COLUMNS`` columns collapse the objects below the deepest level that
        keeps them within the limit (never the top level)."""
        paths, explode = self._paths(None)
        if len(paths) <= MAX_AUTO_COLUMNS:
            return paths, explode
        deepest = max(len(p) for p in paths)
        if deepest <= 1:            # every column is at the top level: nothing to collapse
            return paths, explode
        lo, hi, best = 1, deepest - 1, 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if len(self._paths(mid)[0]) <= MAX_AUTO_COLUMNS:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        self.capped = best
        return self._paths(best)


def _discover(doc: Document, src: Any, count: int, sample: int | None, rows: bool, explode: tuple | None,
              cancel: CancelToken | None, on_block: Callable[[Any], None] | None = None,
              workers: int = 1) -> _ColumnScan:
    """Scan the selection (every record, or an evenly spread deterministic sample of
    ``sample`` records; huge records are left out) for :class:`_ColumnScan`. ``workers > 1``: a scan of
    every record runs in worker processes (:class:`_ScanPool`; the same result) unless rows mode infers
    the expanded array; when their merge cannot be exact, the scan is done again in this thread."""
    scan = _ColumnScan(rows, explode)
    if count <= 0:
        return scan
    if sample is not None and count > sample:
        positions = [k * count // sample for k in range(sample)]
        indices = positions if src is None else [int(src[p]) for p in positions]
        blocks = doc.iter_blocks(indices, block_bytes=SAMPLE_BLOCK_BYTES, cancel=cancel)
    else:
        if workers > 1 and not (rows and explode is None):
            pool = _ScanPool(doc, src, rows, explode, cancel, on_block, workers)
            merged = pool.run()
            if merged is not None:
                return merged
            on_block = pool.catch_up(on_block)
        blocks = doc.iter_blocks(src, block_bytes=BLOCK_BYTES, cancel=cancel)
    member = doc.member_records
    split = doc.split_member
    add = scan.add
    oloads = orjson.loads
    oerror = orjson.JSONDecodeError
    for blk in blocks:
        if not blk.huge:
            sp = _pairs(blk.spans)
            data = blk.data
            for k in range(len(blk.indices)):
                if k & (CHECK_EVERY - 1) == CHECK_EVERY - 1:
                    check(cancel)
                raw = data[sp[2 * k]:sp[2 * k + 1]]
                if member:
                    raw = raw[split(raw)[1]:]
                try:
                    value = oloads(raw)          # (as _scan_loads, inlined: the hot loop of the scan)
                except oerror:
                    try:
                        value = codec.loads(raw)
                    except _DECODE_ERRORS:
                        scan.undecodable += 1     # skipped (and reported) by the export pass
                        continue
                add(value)
        if on_block is not None:
            on_block(blk)
    return scan


def _column_text(path: tuple) -> str:
    return format_path(path) if path else RECORD_COLUMN


def discover_columns(doc: Document, source: Any = None, *, sample: int | None = None, array_mode: str = "join",
                     explode: str | None = None, progress: Progress | None = None,
                     cancel: CancelToken | None = None, workers: int | None = None) -> list[str]:
    """CSV/XLSX columns of ``doc`` (see the module docstring), exactly those of an export
    with automatic columns: the leaf paths of every record of ``source`` (``sample=N``: of
    an evenly spread, deterministic sample of N records instead) in first-seen order, as
    path texts (``contact.address.city``, ``tags``, ``orders[*].oid`` in rows mode, ``$``
    for scalar records, ``attrs.*`` / ``*`` for the pooled keys of a map -- as an explicit
    column that text keeps its wildcard meaning); ``$key`` first for member records. Builds the index when needed;
    huge records and records that cannot be decoded in memory are not scanned. Progress
    as for :func:`export` (one pass). ``workers``: worker processes for a scan of every record (``None``:
    automatic, as :func:`export`; the columns are the same either way)."""
    mode = _choice("array_mode", array_mode, ARRAY_MODES, "writer.expected.array_mode")
    explode_text = _norm_explode(explode)
    if explode_text is not None:
        mode = "rows"
    if sample is not None and (isinstance(sample, bool) or not isinstance(sample, int) or sample < 1):
        raise _invalid("sample", sample, "writer.expected.sample")
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int) or workers < 1):
        raise _invalid("workers", workers, "writer.expected.workers")
    doc = _pin(doc, source)
    _ensure_index(doc, progress, cancel)
    src, count = _normalize_source(doc, source)
    explode_path = tuple(parse_path(explode_text)) if explode_text is not None else None
    meter = _Meter(progress, doc, src, count)
    if progress is not None:
        progress.set_message(t("writer.progress.columns"))
        meter.start(1)
    everything = sample is None or count <= sample
    nworkers = _auto_workers(workers, count, meter.unit, doc.settings) if everything else 1
    scan = _discover(doc, src, count, sample, mode == "rows", explode_path, cancel,
                     meter.advance if everything else None, workers=nworkers)
    if progress is not None:
        progress.finish()
    paths, _ = scan.columns()
    columns = [KEY_COLUMN] if doc.member_records else []
    columns.extend(_column_text(p) for p in paths)
    return columns


# ----------------------------------------------------------------------------------
# export context
# ----------------------------------------------------------------------------------
class _Sink:
    """A binary file that ignores empty writes (the Reformatter often returns ``b""``);
    ``gzip``: the bytes are compressed (``zlib.compressobj(GZIP_LEVEL, DEFLATED, 31)``: one gzip
    member, header mtime 0) -- :meth:`finish` writes the end of the stream. The compressor is fed
    pieces of exactly :data:`GZIP_FEED` bytes of the plain stream (and the rest at the end): zlib's
    output depends on how its input is cut, so this way the compressed bytes depend only on the
    data, never on the sizes of the writes (the CSV rows of worker processes arrive in other pieces
    than the one-thread export writes them; the files are byte for byte the same)."""

    __slots__ = ("f", "z", "pend")

    def __init__(self, f: Any, gzip: bool = False) -> None:
        self.f = f
        self.z = zlib.compressobj(GZIP_LEVEL, zlib.DEFLATED, 31) if gzip else None
        self.pend = bytearray()

    def write(self, data: bytes) -> None:
        if data:
            z = self.z
            if z is None:
                self.f.write(data)
                return
            pend = self.pend
            pend += data
            if len(pend) >= GZIP_FEED:
                n = len(pend) - len(pend) % GZIP_FEED
                with memoryview(pend) as view:
                    for i in range(0, n, GZIP_FEED):
                        out = z.compress(view[i:i + GZIP_FEED])
                        if out:
                            self.f.write(out)
                del pend[:n]

    def finish(self) -> None:
        z = self.z
        if z is not None:
            self.z = None
            if self.pend:
                out = z.compress(self.pend)
                if out:
                    self.f.write(out)
                self.pend = bytearray()
            self.f.write(z.flush())


@contextlib.contextmanager
def _output(out: Path, gzip: bool = False) -> Iterator[_Sink]:
    """``atomic_write(out)`` behind a :class:`_Sink` (``gzip``: compressed); the gzip stream is
    finished when the block ends without an error, then the file is committed."""
    with atomic_write(out, "wb", buffering=WRITE_BUFFER) as f:
        sink = _Sink(f, gzip)
        yield sink
        sink.finish()


class _Ctx:
    """State shared by the exporters of one :func:`export` call."""

    def __init__(self, doc: Document, opts: ExportOptions, src: Any, count: int, progress: Progress | None,
                 cancel: CancelToken | None, out: Path) -> None:
        self.doc = doc
        self.opts = opts
        self.src = src
        self.count = count
        self.progress = progress
        self.cancel = cancel
        self.out = out
        self.member = doc.member_records
        s = doc.settings
        self.large = s.large_node_bytes
        # memory budget (Settings.memory_limit_bytes): cells per row batch, captured bytes
        self.cell_budget = max(MIN_CELL_BUDGET, s.memory_limit_bytes // CELL_BUDGET_DIVISOR)
        self.capture_limit = max(CAPTURE_MIN_BYTES, min(s.large_node_bytes, s.memory_limit_bytes // 4))
        # lone surrogates can only come from \u escapes (doc.flags covers keys and values)
        self.check_surrogates = bool(doc.flags & FLAG_U_ESCAPES)
        self.meter = _Meter(progress, doc, src, count)
        self.records = 0
        self.rows = 0
        self.skipped = 0
        self.columns: list[_Column] = []
        self.explode: tuple | None = None
        self.tables: list[str] = []
        self.sorter: _KeySorter | None = None
        self.gzip = False                       # the output is compressed (a ".gz" name)
        self.want_workers: int | None = None    # export(workers=): None = automatic (_pool_workers)
        self.workers = 1                        # worker processes used (ExportReport.workers)
        self._warnings: dict[str, dict[str, Any]] = {}
        self._fh: Any = None

    # -- warnings --------------------------------------------------------------------
    def warn(self, key: str, add: int = 1, first: dict[str, Any] | None = None, **params: Any) -> None:
        """Count a warning; ``params`` are updated every time, ``first`` only once."""
        entry = self._warnings.get(key)
        if entry is None:
            entry = self._warnings[key] = {"count": 0}
            if first:
                entry.update(first)
        entry["count"] += add
        entry.update(params)

    def warning_texts(self) -> tuple[list[str], list[str]]:
        keys = list(self._warnings)
        return [_tn(k, **self._warnings[k]) for k in keys], keys

    def warning_params(self) -> list[dict[str, Any]]:
        """The parameters of the warnings, in the order of :meth:`warning_texts` (copies)."""
        return [dict(entry) for entry in self._warnings.values()]

    def skip_huge(self, key: str) -> None:
        self.skipped += 1
        self.warn(key, limit=format_bytes(self.large))

    def skip_deep(self, key: str) -> None:
        self.skipped += 1
        self.warn(key)

    def text_cap(self, n: int) -> int:
        """Characters a cell built from a stream may hold (huge records whose selected
        values exceed :attr:`capture_limit`): Excel's limit for XLSX, else the capture
        budget shared by the ``n`` path columns read together (at least CELL_TEXT_MIN)."""
        if self.opts.fmt == "xlsx":
            return XLSX_MAX_STRING
        return max(CELL_TEXT_MIN, self.capture_limit // max(1, n))

    # -- progress ----------------------------------------------------------------------
    def note_source(self) -> None:
        """The warnings about the source itself: partial, broken JSON Lines lines, a wrapper the
        format has no place for (JSON Lines / CSV / XLSX; SQLite keeps it in ``_wrapper``)."""
        doc = self.doc
        if doc.error is not None:
            self.warn("writer.warn.partial", add=0)   # count: records exported (see finish())
        if doc.kind == "jsonl" and doc.broken_count:
            self.warn("writer.warn.broken_lines", add=doc.broken_count)
        if doc.kind == "wrapped" and self.opts.keep_wrapper and self.opts.fmt in ("jsonl", "csv", "xlsx", "parquet"):
            self.wrapper_not_written()

    def wrapper_not_written(self) -> None:
        """``writer.note.wrapper_not_written``: the bytes around the record container were not written."""
        path = self.doc.info.record_path_text or "$"
        self.warn("writer.note.wrapper_not_written", fmt=FORMAT_NAMES[self.opts.fmt], path=path)

    def begin(self, passes: int = 1) -> None:
        self.note_source()
        self.meter.start(passes)
        if self.progress is not None:
            self.progress.set_message(t("writer.progress.export", name=self.out.name))

    def finish(self) -> None:
        partial = self._warnings.get("writer.warn.partial")
        if partial is not None:
            partial["count"] = self.records
        p = self.progress
        if p is not None:
            if p.total is None or p.done > p.total:
                p.set_total(p.done)
            p.finish()

    def message(self, key: str) -> None:
        if self.progress is not None:
            self.progress.set_message(t(key, name=self.out.name))

    def advance(self, blk: Block) -> None:
        self.meter.advance(blk)

    def next_pass(self) -> None:
        self.meter.next_pass()

    def finishing(self) -> Callable[[int], None] | None:
        """The final, slow step of writing a file (XLSX zip assembly): progress in bytes
        written, total unknown. Returns the callback for the bytes written so far."""
        p = self.progress
        if p is None:
            return None
        p.set_message(t("writer.progress.finish", name=self.out.name))
        p.set_total(None)
        p.update(0)
        p.restart_clock()
        return p.update

    def batch_rows(self, width: int) -> int:
        """Rows per batch for rows of ``width`` cells (cell budget, at most ROW_BATCH)."""
        return max(1, min(ROW_BATCH, self.cell_budget // max(1, width)))

    # -- reading -----------------------------------------------------------------------
    def blocks(self) -> Iterator[Block]:
        return self.doc.iter_blocks(self.src, block_bytes=BLOCK_BYTES, cancel=self.cancel)

    def chunks(self, start: int, end: int) -> Iterator[bytes]:
        """Bytes ``[start, end)`` of the document in pieces of :data:`CHUNK_BYTES`, read through
        ``doc.open_data()`` (the virtual address space: an edited huge record or wrapper lives in
        the edit layer)."""
        if self._fh is None:
            open_data = getattr(self.doc, "open_data", None)
            self._fh = open_data() if open_data is not None else open_shared(self.doc.data_path, "rb")
        pos = start
        while pos < end:
            check(self.cancel)
            stop = min(end, pos + CHUNK_BYTES)
            yield read_range(self._fh, pos, stop)
            pos = stop

    def stream_values(self, span: tuple[int, int], patterns: list[PathPattern],
                      member: bool = False) -> dict[int, list[Any]]:
        """``stream.stream_values`` with the capture limit (when the stream module has one)."""
        if _STREAM_MAX_BYTES:
            return stream_values(self.doc, span, patterns, member=member, max_bytes=self.capture_limit)
        return stream_values(self.doc, span, patterns, member=member)

    def commit_check(self, targets: Sequence[Path] | None = None) -> None:
        """Right before a result is committed: not cancelled, source unchanged, and no target (``targets``,
        default the output) got unsaved edits meanwhile (:func:`refuse_edited_targets`)."""
        check(self.cancel)
        self.doc.ensure_unchanged()
        refuse_edited_targets([self.out] if targets is None else targets)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:  # pragma: no cover
                pass
            self._fh = None


class _PathDoc:
    """What :func:`jsonreshape.editstore.find_pending` reads of a document, for a file that is not opened here:
    its path and an identity that matches no layer -- so every layer registered for the path or kept next to
    it is reported (live ones as ``locked``, pending ones with their steps)."""

    index_complete = True
    closed = False

    def __init__(self, path: Path) -> None:
        self.source_path = path
        self.identity = FileIdentity(path=normalize_path(path), size=-1, mtime_ns=-1, head_hash="")
        self.info = SimpleNamespace(kind="", record_path=None)
        self.base_record_count = self.record_count = 0
        self.data_size = 0
        self.settings = None


def edited_targets(paths: Iterable[StrPath]) -> list[Path]:
    """The existing files of ``paths`` that have unsaved edits in this program: an edit layer registered for
    that path or kept next to it (``<file>.jredit``) -- live (a tab or another instance edits it: its lock is
    held) or pending (kept for recovery). Replacing or removing such a file would leave edits that can never
    be saved (fix round R5, R5F-3; the rule of split parts, every writer output since fix round L1).

    :func:`jsonreshape.editstore.layer_sources` (the layer index, read once) names the candidates and
    :func:`jsonreshape.editstore.find_pending` decides about each of them, so files without a layer cost a
    ``stat`` each. A file shown in a tab without edits is not edited (it is replaced as before; the tab sees
    the change). An unusable edit-layer folder: no layer can be found (nothing is refused). The files are
    returned as given (as :class:`~pathlib.Path`), in order."""
    existing = [Path(os.fspath(p)) for p in paths if os.path.isfile(long_path(p))]
    if not existing:
        return []
    from . import editstore     # lazy: editstore imports much of the package

    sources = editstore.layer_sources()
    out = []
    for p in existing:
        doc = _PathDoc(p)
        if normalize_path(p) not in sources and not os.path.isdir(long_path(editstore.beside_dir(doc))):
            continue
        try:
            infos = editstore.find_pending(doc)
        except JsonReshapeError:
            log.debug("looking for edit layers of %s failed", p, exc_info=True)
            continue
        if infos:
            out.append(p)
    return out


def refuse_edited_targets(paths: Iterable[StrPath]) -> None:
    """``UserInputError("filetools.error.part_open_edited")`` naming the files of ``paths`` that have unsaved
    edits (:func:`edited_targets`; at most three named, then ``…``); nothing when there are none."""
    edited = edited_targets(paths)
    if edited:
        shown = ", ".join(str(p) for p in edited[:3]) + (", \N{HORIZONTAL ELLIPSIS}" if len(edited) > 3 else "")
        raise UserInputError("filetools.error.part_open_edited", path=shown, count=len(edited))


def _check_target(doc: Document, out: Path) -> None:
    """``out`` may be written: not the source or the document's data file (also under another
    name: ``os.path.samefile``), not a folder, in a folder that exists."""
    target = normalize_path(out)
    for p in (doc.source_path, doc.data_path):
        if target == normalize_path(p):
            raise UserInputError("writer.error.same_file", path=str(out))
    lp = long_path(out)
    if os.path.isdir(lp):
        raise UserInputError("fileio.error.is_directory", path=str(out))
    if os.path.exists(lp):
        for p in dict.fromkeys((doc.source_path, doc.data_path)):
            try:
                same = os.path.samefile(lp, long_path(p))
            except OSError:
                same = False
            if same:
                raise UserInputError("writer.error.same_file", path=str(out))
    if not os.path.isdir(long_path(out.parent)):
        raise UserInputError("writer.error.no_dir", path=str(out.parent))


@dataclass
class ColumnPlan:
    """CSV/XLSX columns found once (:func:`plan_columns`) and shared by several exports of the
    same document and options -- every part of a split gets the same header, exactly the
    automatic columns one export of the whole selection would have. ``header``: the column
    texts; ``warnings`` / ``warning_keys``: what the column scan reported (map columns, deep
    columns, the column cap), reported once by the caller. Internal fields are opaque."""

    header: list[str]
    warnings: list[str]
    warning_keys: list[str]
    _columns: list[Any] = field(repr=False, default_factory=list)
    _explode: tuple | None = field(repr=False, default=None)


def plan_columns(doc: Document, opts: ExportOptions, source: Any = None, *, progress: Progress | None = None,
                 cancel: CancelToken | None = None) -> ColumnPlan:
    """The CSV/XLSX columns an :func:`export` of ``source`` with ``opts`` would use (automatic
    columns: one scan of every selected record, like the export's first pass -- in worker processes
    by the same automatic rule; explicit columns: parsed, and in rows mode the expanded array
    found), to be passed as ``column_plan=`` to exports of parts of that selection. Progress: one
    pass, as :func:`export`."""
    if not isinstance(opts, ExportOptions):
        raise TypeError("opts must be an ExportOptions instance")
    opts.validate()
    if opts.fmt not in TABLE_FORMATS:
        raise _invalid("fmt", opts.fmt, "writer.expected.table_fmt")
    doc = _pin(doc, source)
    _ensure_index(doc, progress, cancel)
    src, count = _normalize_source(doc, source)
    ctx = _Ctx(doc, opts, src, count, progress, cancel, Path(doc.source_path))
    try:
        ctx.meter.start(1)
        _prepare_columns(ctx)
    finally:
        ctx.close()
    texts, keys = ctx.warning_texts()
    if progress is not None:
        progress.finish()
    return ColumnPlan(header=[c.text for c in ctx.columns], warnings=texts, warning_keys=keys,
                      _columns=list(ctx.columns), _explode=ctx.explode)


def _scan_passes(opts: ExportOptions) -> int:
    """2 when CSV/XLSX columns (or the expanded array) must be found by a scan first, and
    for SQLite (its map detection)."""
    if opts.fmt == "sqlite":
        return 2
    if opts.fmt not in TABLE_FORMATS:
        return 1
    if opts.columns is None:
        return 2
    if opts.array_mode == "rows" and opts.explode is None \
            and _explode_from_columns([_parse_column(c) for c in opts.columns]) is None:
        return 2
    return 1


def _check_gzip(opts: ExportOptions, out: Path, gzip: bool | None) -> bool:
    """Whether the output is compressed: ``gzip``, else its name ends with ``.gz``; only JSON,
    JSON Lines and CSV can be (``writer.error.gzip_format``)."""
    gz = gzip_path(out) if gzip is None else bool(gzip)
    if gz and opts.fmt not in GZIP_FORMATS:
        raise UserInputError("writer.error.gzip_format", fmt=FORMAT_NAMES[opts.fmt], path=str(out))
    return gz


def export(doc: Document, out_path: StrPath, opts: ExportOptions, source: Any = None, *,
           progress: Progress | None = None, cancel: CancelToken | None = None, gzip: bool | None = None,
           column_plan: ColumnPlan | None = None, workers: int | None = None) -> ExportReport:
    """Write the records of ``doc`` (all, or the ``source`` selection in its order) to
    ``out_path`` as described by ``opts`` (see the module docstring). The file appears
    atomically; on an error, a cancellation or a change of the source no temporary file is
    left and an existing target is unchanged. Returns an :class:`ExportReport`.

    ``gzip``: compress the JSON / JSON Lines / CSV output (default: when ``out_path`` ends with
    ``.gz``). ``column_plan``: CSV/XLSX columns found by :func:`plan_columns` for a selection this
    ``source`` is a part of (no column scan; the plan's own warnings are not repeated).
    ``workers``: worker processes for the CSV rows and the CSV/XLSX column scan (``None``: automatic;
    ``1``: the calling thread only; the file is the same either way -- module docstring, "CSV and
    XLSX"). The other formats ignore it."""
    if not isinstance(opts, ExportOptions):
        raise TypeError("opts must be an ExportOptions instance")
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int) or workers < 1):
        raise _invalid("workers", workers, "writer.expected.workers")
    opts.validate()
    doc = _pin(doc, source)
    started = time.perf_counter()
    out = Path(os.path.abspath(os.fspath(out_path)))
    _check_target(doc, out)
    gz = _check_gzip(opts, out, gzip)
    if opts.fmt == "parquet":            # the optional pyarrow package, before any work
        from .writer_parquet import require_pyarrow

        require_pyarrow()
    refuse_edited_targets([out])         # (the XLSX "files" parts are checked once they are named)
    _purge_staging(doc.settings)
    _ensure_index(doc, progress, cancel)
    src, count = _normalize_source(doc, source)
    ctx = _Ctx(doc, opts, src, count, progress, cancel, out)
    ctx.gzip = gz
    ctx.want_workers = workers
    planned = column_plan is not None and opts.fmt in TABLE_FORMATS
    try:
        ctx.begin(1 if planned else _scan_passes(opts))
        if planned:
            assert column_plan is not None
            ctx.columns = list(column_plan._columns)
            ctx.explode = column_plan._explode
        elif opts.fmt in TABLE_FORMATS:
            _prepare_columns(ctx)
        files = _EXPORTERS[opts.fmt](ctx, out)
    finally:
        ctx.close()
    ctx.finish()
    texts, keys = ctx.warning_texts()
    size = 0
    for p in files:
        try:
            size += os.stat(long_path(p)).st_size
        except OSError:  # pragma: no cover - just written
            pass
    report = ExportReport(
        fmt=opts.fmt,
        records=ctx.records,
        rows=ctx.rows,
        bytes_written=size,
        files=files,
        warnings=texts,
        elapsed=time.perf_counter() - started,
        skipped=ctx.skipped,
        columns=[c.text for c in ctx.columns],
        tables=list(ctx.tables),
        warning_keys=keys,
        warning_params=ctx.warning_params(),
        workers=ctx.workers,
    )
    log.info("exported %d records (%d rows) of %s to %s as %s: %d bytes in %.2f s%s%s", report.records, report.rows,
             doc.source_path.name, out, opts.fmt, size, report.elapsed,
             f" ({ctx.workers} worker processes)" if ctx.workers > 1 else "",
             f"; warnings: {', '.join(keys)}" if keys else "")
    return report


# ----------------------------------------------------------------------------------
# JSON / JSON Lines
# ----------------------------------------------------------------------------------
def _consecutive(indices: array) -> bool:
    return len(indices) == 0 or indices[-1] - indices[0] == len(indices) - 1


def _feed_all(feed: Callable[[bytes], bytes], data: bytes, sink: _Sink) -> None:
    """Feed ``data`` to a Reformatter in :data:`FEED_BYTES` pieces (tokens may be split)."""
    if len(data) <= FEED_BYTES:
        sink.write(feed(data))
        return
    for i in range(0, len(data), FEED_BYTES):
        sink.write(feed(data[i:i + FEED_BYTES]))


_UNSET = object()


class _KeySorter:
    """Puts the keys of every object of a value in alphabetical order, in place
    (:func:`jsonreshape.sortkeys.collation_key`: Uzbek collation, natural order,
    case-insensitive; the order is total). Iterative: any nesting depth."""

    KEYS_MAX = 100_000
    ORDERS_MAX = 10_000

    def __init__(self) -> None:
        self._keys: dict[str, bytes] = {}
        self._orders: dict[tuple, tuple | None] = {}

    def _key(self, s: str) -> bytes:
        k = self._keys.get(s)
        if k is None:
            if len(self._keys) >= self.KEYS_MAX:
                self._keys.clear()
            k = self._keys[s] = collation_key(s)
        return k

    def _order(self, keys: tuple) -> tuple | None:
        """The sorted keys, or ``None`` when ``keys`` already are in order."""
        o = self._orders.get(keys, _UNSET)
        if o is _UNSET:
            s = tuple(sorted(keys, key=self._key))
            o = None if s == keys else s
            if len(self._orders) >= self.ORDERS_MAX:
                self._orders.clear()
            self._orders[keys] = o
        return o  # type: ignore[return-value]

    def sort(self, value: Any) -> None:
        if type(value) is not dict and type(value) is not list:
            return
        stack = [value]
        while stack:
            v = stack.pop()
            if type(v) is dict:
                if len(v) > 1:
                    o = self._order(tuple(v))
                    if o is not None:
                        pop = v.pop
                        for k in o:
                            v[k] = pop(k)
                for x in v.values():
                    if type(x) is dict or type(x) is list:
                        stack.append(x)
            else:
                for x in v:
                    if type(x) is dict or type(x) is list:
                        stack.append(x)


def _sorted_record(ctx: _Ctx, raw: bytes, member: bool | None = None) -> bytes:
    """``sort_keys``: the record re-encoded with sorted keys (member records -- and the
    members of ``as_keyed`` records, ``member=True`` -- keep their raw key part
    ``"key":``); a record that cannot be decoded in memory stays as it is."""
    head = b""
    body = raw
    if ctx.member if member is None else member:
        _, vo = ctx.doc.split_member(raw)
        head, body = raw[:vo], raw[vo:]
    try:
        value = codec.loads(body)
    except _DECODE_ERRORS:
        ctx.warn("writer.warn.deep_unsorted")
        return raw
    # the result is kept with the block's other pieces: a compact text (head + text copies already)
    dumps = codec.dumps_transient if head else codec.dumps
    if ctx.opts.sort_keys == "codepoint":
        return head + dumps(value, sort_keys=True)
    if ctx.sorter is None:
        ctx.sorter = _KeySorter()
    ctx.sorter.sort(value)
    return head + dumps(value)


def _record_pieces(ctx: _Ctx, blk: Block) -> list[bytes]:
    sp = _pairs(blk.spans)
    data = blk.data
    n = len(blk.indices)
    pieces = [data[sp[2 * k]:sp[2 * k + 1]] for k in range(n)]
    if ctx.opts.sort_keys:
        out = []
        for k, raw in enumerate(pieces):
            if k & (CHECK_EVERY - 1) == CHECK_EVERY - 1:
                check(ctx.cancel)
            out.append(_sorted_record(ctx, raw))
        return out
    return pieces


def _wrapper_spans(ctx: _Ctx) -> tuple[tuple[int, int], tuple[int, int]] | None:
    doc = ctx.doc
    if doc.kind != "wrapped" or not ctx.opts.keep_wrapper or ctx.opts.as_keyed:
        return None
    spans = doc.wrapper_spans(cancel=ctx.cancel) if doc.error is None else None
    if spans is None:
        ctx.warn("writer.warn.wrapper_dropped")
    return spans


def _json_records(ctx: _Ctx, feed: Callable[[bytes], bytes], sink: _Sink, first: bool,
                  wrap_members: bool = False, keys: _MemberKeys | None = None) -> bool:
    """The records of ``ctx`` fed to one Reformatter as JSON array items / object members joined
    with ``,`` (``first``: no ``,`` before the first one); returns the new ``first``.
    ``wrap_members``: member records written as one-member objects ``{"key": value}`` (an array
    root); ``keys``: the member keys are counted for the repeated-key warning (an object root)."""
    opts = ctx.opts
    whole_blocks = ctx.doc.kind in _COMMA_KINDS and not opts.sort_keys and not wrap_members and keys is None
    for blk in ctx.blocks():
        if blk.huge:
            if not first:
                sink.write(feed(b","))
            if opts.sort_keys:
                ctx.warn("writer.warn.huge_unsorted")
            if wrap_members:
                sink.write(feed(b"{"))
            head = True
            for chunk in ctx.chunks(*blk.span(0)):
                if head and keys is not None:
                    keys.add_huge(ctx.doc, chunk, ctx.records)
                head = False
                sink.write(feed(chunk))
            if wrap_members:
                sink.write(feed(b"}"))
        elif whole_blocks and blk.contiguous and _consecutive(blk.indices):
            # consecutive records of a comma-separated source: the block holds the
            # records and their separators (',' + whitespace) and nothing else
            if not first:
                sink.write(feed(b","))
            _feed_all(feed, blk.data, sink)
        else:
            pieces = _record_pieces(ctx, blk)
            if keys is not None:
                keys.add_members(ctx.doc, pieces, ctx.records)
            if wrap_members:
                data = b"{" + b"},{".join(pieces) + b"}"
            else:
                data = b",".join(pieces)
            _feed_all(feed, data if first else b"," + data, sink)
        first = False
        ctx.records += len(blk.indices)
        ctx.advance(blk)
    return first


def _jsonl_records(ctx: _Ctx, feed: Callable[[bytes], bytes], sink: _Sink) -> None:
    """The records of ``ctx`` as JSON Lines: one compact line each (member records as
    ``{"key":value}``), ``\\n`` after every line."""
    member = ctx.member
    for blk in ctx.blocks():
        if blk.huge:
            if ctx.opts.sort_keys:
                ctx.warn("writer.warn.huge_unsorted")
            if member:
                sink.write(b"{")
            for chunk in ctx.chunks(*blk.span(0)):
                sink.write(feed(chunk))
            sink.write(b"}\n" if member else b"\n")
        else:
            parts: list[bytes] = []
            if member:
                for piece in _record_pieces(ctx, blk):
                    parts += (b"{", feed(piece), b"}\n")
            else:
                for piece in _record_pieces(ctx, blk):
                    parts += (feed(piece), b"\n")
            sink.write(b"".join(parts))
        ctx.records += len(blk.indices)
        ctx.advance(blk)


def _export_json(ctx: _Ctx, out: Path) -> list[Path]:
    doc, opts = ctx.doc, ctx.opts
    if opts.as_keyed and not ctx.member:
        return _export_keyed(ctx, out)
    rf = core.Reformatter(opts.indent)
    feed = rf.feed
    bare = doc.kind == "single" and ctx.count == 1
    wrapper = _wrapper_spans(ctx)
    open_b, close_b = (b"{", b"}") if ctx.member else (b"[", b"]")
    with _output(out, ctx.gzip) as sink:
        if wrapper is not None:
            for chunk in ctx.chunks(*wrapper[0]):
                sink.write(feed(chunk))
        if not bare:
            sink.write(feed(open_b))
        _json_records(ctx, feed, sink, True)
        if not bare:
            sink.write(feed(close_b))
        if wrapper is not None:
            for chunk in ctx.chunks(*wrapper[1]):
                sink.write(feed(chunk))
        sink.write(rf.finish())
        ctx.commit_check()
    ctx.rows = ctx.records
    return [out]


# ----------------------------------------------------------------------------------
# JSON: one keyed object from one-member records (as_keyed)
# ----------------------------------------------------------------------------------
_SHAPE_KEYS = {
    "array": "writer.shape.array",
    "string": "writer.shape.string",
    "number": "writer.shape.number",
    "bool": "writer.shape.bool",
    "null": "writer.shape.null",
}
_SHAPE_BY_BYTE = {ord("["): "array", ord('"'): "string", ord("t"): "bool", ord("f"): "bool", ord("n"): "null"}
_KEY_TOKEN_RE = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"', re.S)
DUP_KEY_SHOWN = 60                # characters of the repeated key named in the warning


def _feed_pieces(data: bytes, size: int = FEED_BYTES) -> list[bytes]:
    """``data`` in pieces of at most ``size`` bytes (core feeds; see ``_feed_all``)."""
    if len(data) <= size:
        return [data]
    return [data[i:i + size] for i in range(0, len(data), size)]


def _not_single_member(ctx: _Ctx, index: int, kind: str, members: int | None = None) -> ExportError:
    """``writer.error.not_single_member`` for record ``index``: ``kind`` is ``"object"``
    (with its number of ``members``) or the kind of a value that is not an object. A source
    changed by another program raises :class:`~jsonreshape.errors.FileChangedError` instead
    (the real cause of unexpected bytes)."""
    ctx.doc.ensure_unchanged()
    if kind == "object":
        what = t("writer.shape.empty") if members == 0 else t("writer.shape.members", count=members)
    else:
        what = t(_SHAPE_KEYS[kind])
    return ExportError("writer.error.not_single_member", index=index, what=what, kind=kind, members=members)


def _record_shape(raw: bytes) -> tuple[str, int | None] | None:
    """``None`` when ``raw`` (one JSON value) is an object with exactly one member, else
    ``(kind, members)`` describing it (``members`` only for objects)."""
    if not raw.startswith(b"{"):
        return _SHAPE_BY_BYTE.get(raw[0] if raw else 0, "number"), None
    sc = core.Scanner(MODE_CONTAINER, [])
    for piece in _feed_pieces(raw):
        sc.feed(piece)
    sc.finish()
    return None if sc.error is None and sc.record_count == 1 else ("object", sc.record_count)


def _block_error(ctx: _Ctx, blk: Block) -> ExportError:
    """The error for the first record of ``blk`` that is not a one-member object."""
    sp = _pairs(blk.spans)
    data = blk.data
    for k in range(len(blk.indices)):
        if k & (CHECK_EVERY - 1) == CHECK_EVERY - 1:
            check(ctx.cancel)
        shape = _record_shape(data[sp[2 * k]:sp[2 * k + 1]])
        if shape is not None:
            return _not_single_member(ctx, int(blk.indices[k]), *shape)
    ctx.doc.ensure_unchanged()
    return ExportError("error.export", detail="as_keyed: the members found do not match the records")


class _DupKeys:
    """Member keys repeated across the records of an ``as_keyed`` export. They are written
    as they are (JSON allows repeated keys; the round trip stays faithful) and counted for
    ``writer.warn.duplicate_keys`` -- exactly (keys compared after decoding escapes) while
    at most :data:`DUP_KEYS_MAX` keys taking at most ``memory_limit_bytes //
    DUP_MEMORY_DIVISOR`` bytes (estimated) are remembered; later keys are not checked and
    the warning says how many were (no warning when no repeated key was found)."""

    def __init__(self, doc: Document) -> None:
        self.seen: set[bytes] | None = set()
        self.budget = max(DUP_KEY_OVERHEAD, doc.settings.memory_limit_bytes // DUP_MEMORY_DIVISOR)
        self.used = 0
        self.checked = 0
        self.count = 0
        self.first: tuple[int, bytes] | None = None
        self.limited = False
        self.escapes = bool(doc.flags & FLAG_ESCAPES)

    def _room(self) -> int:
        return max(0, min(DUP_KEYS_MAX - self.checked, (self.budget - self.used) // DUP_KEY_OVERHEAD))

    def add_block(self, body: bytes, spans: bytes, indices: Any) -> None:
        """The keys of a block: ``body`` = the members joined with ``,``; ``spans`` = the
        member spans a scanner found in ``b"{" + body + b"}"`` (each starts at the opening
        quote of its key, i.e. one byte after it in ``body``), one per record of ``indices``."""
        if self.seen is None:
            self.limited = True
            return
        starts = _pairs(spans)[0::2]
        room = self._room()
        cut = len(starts) > room
        if cut:
            starts = starts[:room]
            indices = indices[:room]
        find = body.find
        keys = [body[s:find(b'"', s)] for s in starts]
        if self.escapes:
            for j, k in enumerate(keys):
                if b"\\" in k:   # escapes (or an escaped quote): decode the whole token
                    m = _KEY_TOKEN_RE.match(body, starts[j] - 1)
                    keys[j] = codec.loads(m.group() if m else b'""').encode("utf-8", "surrogatepass")
        self._add(keys, indices)
        if cut:
            self.limited = True
            self.seen = None

    def add_huge(self, doc: Document, head: bytes, index: int) -> None:
        """The key of a huge record from its first piece ``head`` (``{`` + the member)."""
        if self.seen is None or self._room() < 1:
            self.limited = True
            self.seen = None
            return
        try:
            key, _ = doc.split_member(head[1:])
        except ValueError:   # the key does not end in the first piece: not checked
            self.limited = True
            return
        self._add([key.encode("utf-8", "surrogatepass")], [index])

    def _add(self, keys: list[bytes], indices: Any) -> None:
        seen = self.seen
        assert seen is not None
        before = len(seen)
        if seen.isdisjoint(keys):
            seen.update(keys)
            if len(seen) - before != len(keys):   # repeated inside the block: none was seen before
                seen.difference_update(keys)
                self._slow(seen, keys, indices)
        else:
            self._slow(seen, keys, indices)
        self.checked += len(keys)
        self.used += sum(map(len, keys)) + DUP_KEY_OVERHEAD * len(keys)
        if self.checked >= DUP_KEYS_MAX or self.used >= self.budget:
            self.seen = None   # stop checking (and free the memory)

    def _slow(self, seen: set[bytes], keys: list[bytes], indices: Any) -> None:
        for j, k in enumerate(keys):
            if k in seen:
                self.count += 1
                if self.first is None:
                    self.first = (int(indices[j]), k)
            else:
                seen.add(k)

    def report(self, ctx: _Ctx, key: str = "writer.warn.duplicate_keys") -> None:
        if not self.count or self.first is None:
            return
        index, raw_key = self.first
        text = raw_key.decode("utf-8", "surrogatepass")
        if len(text) > DUP_KEY_SHOWN:
            text = text[:DUP_KEY_SHOWN] + "\N{HORIZONTAL ELLIPSIS}"
        note = t("writer.note.duplicate_keys_limited", checked=self.checked) if self.limited else ""
        ctx.warn(key, add=self.count, first={"index": index, "key": codec.dumps(text).decode("utf-8")}, note=note)


class _MemberKeys(_DupKeys):
    """Member keys repeated across the records written into ONE object (an object-root
    :func:`export_many`: several keyed / wrapped-object inputs): counted exactly like
    :class:`_DupKeys` (decoded keys, within the same memory budget). ``offset``: output position
    of the current input's first record (the warning names the output position)."""

    def __init__(self, doc: Document) -> None:
        super().__init__(doc)
        self.offset = 0

    def add_members(self, doc: Document, pieces: list[bytes], base: int) -> None:
        """The keys of member records ``pieces`` (``"key": value``), the first one at position
        ``offset + base`` of the output."""
        if self.seen is None:
            if pieces:
                self.limited = True
            return
        room = self._room()
        cut = len(pieces) > room
        if cut:
            pieces = pieces[:room]
        split = doc.split_member
        keys = [split(raw)[0].encode("utf-8", "surrogatepass") for raw in pieces]
        start = self.offset + base
        self._add(keys, range(start, start + len(keys)))
        if cut:
            self.limited = True
            self.seen = None

    def add_huge(self, doc: Document, head: bytes, index: int) -> None:
        """The key of a huge member record from its first piece ``head`` (``"key": ...``)."""
        if self.seen is None or self._room() < 1:
            self.limited = True
            self.seen = None
            return
        try:
            key, _ = doc.split_member(head)
        except ValueError:   # the key does not end in the first piece: not checked
            self.limited = True
            return
        self._add([key.encode("utf-8", "surrogatepass")], [self.offset + index])


def _joined_members(ctx: _Ctx, blk: Block) -> tuple[bytes, list[bytes] | None]:
    r"""``(body, pieces)``: the records of ``blk`` without their outer bytes (the braces of
    an object), joined with ``,`` -- and the list of those pieces (``None`` when the body was
    made in one step). JSON Lines blocks read as one range whose records are separated by a
    single ``\n`` each take the one-step way: the records are on lines of their own (every
    gap is at least one byte), so gaps of ``n - 1`` bytes in all are one ``\n`` each, the
    only ``\n`` bytes of the range; ``}\n{`` then occurs only between records and replacing
    it ``n - 1`` times proves that every record boundary was ``}`` + ``\n`` + ``{``: the result
    equals the joined pieces."""
    sp = _pairs(blk.spans)
    data = blk.data
    n = len(blk.indices)
    if blk.contiguous and ctx.doc.kind == "jsonl" and not ctx.opts.sort_keys:
        starts = sp[0::2]
        ends = sp[1::2]
        first = starts[0]
        last = ends[-1]
        if (sum(starts) - first) - (sum(ends) - last) == n - 1 and data[first] == 0x7B and data[last - 1] == 0x7D:
            inner = data[first + 1:last - 1]
            body = inner.replace(b"}\n{", b",")
            if len(inner) - len(body) == 2 * (n - 1):
                return body, None
    pieces = [data[s + 1:e - 1] for s, e in zip(sp[0::2], sp[1::2])]
    return b",".join(pieces), pieces


def _check_members(parts: list[bytes], body: bytes, indices: Any, keys: _DupKeys) -> bool:
    """One scanner pass over ``{`` + ``body`` (fed as ``parts``) + ``}``: ``True`` when it is
    an object with one member per record -- which holds exactly when every record is an
    object with exactly one member (a record that is not an object, or an empty one, makes
    the text invalid or short of members; one with n >= 1 members adds exactly n) -- and
    the keys are then counted for the duplicate-key warning. Thread-safe as used: one call
    at a time (the Rust core releases the GIL, so this runs beside the Reformatter)."""
    sc = core.Scanner(MODE_CONTAINER, [])
    found = [sc.feed(b"{")]
    found += [sc.feed(p) for p in parts]
    found.append(sc.feed(b"}"))
    found.append(sc.finish())
    if sc.error is not None or sc.record_count != len(indices):
        return False
    keys.add_block(body, b"".join(found), indices)
    return True


def _sorted_members(ctx: _Ctx, pieces: list[bytes]) -> bytes:
    """``sort_keys``: the checked members with the keys inside their values sorted."""
    out = []
    for k, piece in enumerate(pieces):
        if k & (CHECK_EVERY - 1) == CHECK_EVERY - 1:
            check(ctx.cancel)
        out.append(_sorted_record(ctx, piece, member=True))
    return b",".join(out)


def _keyed_huge(ctx: _Ctx, blk: Block, feed: Callable[[bytes], bytes], sink: _Sink, keys: _DupKeys) -> None:
    """A huge record (``as_keyed``): streamed in CHUNK_BYTES pieces without its braces while
    a scanner counts its members; anything but exactly one member raises (the atomic
    write then saves nothing)."""
    s, e = blk.span(0)
    index = int(blk.indices[0])
    if ctx.opts.sort_keys:
        ctx.warn("writer.warn.huge_unsorted")
    sc = core.Scanner(MODE_CONTAINER, [], s)
    pos = s
    for chunk in ctx.chunks(s, e):
        if pos == s:
            if not chunk.startswith(b"{"):
                raise _not_single_member(ctx, index, _SHAPE_BY_BYTE.get(chunk[0] if chunk else 0, "number"))
            keys.add_huge(ctx.doc, chunk, index)
        sc.feed(chunk)
        end = pos + len(chunk)
        if sc.record_count <= 1 and sc.error is None:   # else: only counting the members
            lo = 1 if pos == s else 0
            hi = len(chunk) - 1 if end == e else len(chunk)
            sink.write(feed(chunk[lo:hi] if lo or hi < len(chunk) else chunk))
        pos = end
    if sc.error is None:
        sc.finish()
    if sc.error is not None:
        ctx.doc.ensure_unchanged()
        raise ExportError("error.export", detail=f"as_keyed: record #{index} could not be read again")
    if sc.record_count != 1:
        raise _not_single_member(ctx, index, "object", sc.record_count)


def _export_keyed(ctx: _Ctx, out: Path) -> list[Path]:
    """``as_keyed`` for value records: ``{`` + the members of the records + ``}`` through one
    Reformatter (tokens byte for byte, like every JSON export); see the module docstring.
    With the Rust core (and no ``sort_keys``) the check of a block (scanner + duplicate
    keys) runs in a helper thread while the main thread reformats and writes the block; the
    block goes to each core call whole (up to 8 MiB; the Rust core releases the GIL for it),
    so the main thread rarely waits for the GIL and the check costs almost no time. A block
    that fails the check raises before anything is committed."""
    rf = core.Reformatter(ctx.opts.indent)
    feed = rf.feed
    keys = _DupKeys(ctx.doc)
    sort = bool(ctx.opts.sort_keys)
    threaded = core.BACKEND == "rust" and not sort
    pool = ThreadPoolExecutor(1, thread_name_prefix="jsonreshape-keyed") if threaded else None
    try:
        with _output(out, ctx.gzip) as sink:
            sink.write(feed(b"{"))
            first = True
            for blk in ctx.blocks():
                if blk.huge:
                    if not first:
                        sink.write(feed(b","))
                    _keyed_huge(ctx, blk, feed, sink, keys)
                    first = False
                elif len(blk.indices):
                    body, pieces = _joined_members(ctx, blk)
                    # one piece per block for the Rust core (8 MiB: few GIL hand-overs; a block
                    # holding one big record is cut too, bounding the output of one feed); the
                    # pure-Python core gets at most FEED_BYTES at a time (its memory)
                    parts = _feed_pieces(body, BLOCK_BYTES if threaded else FEED_BYTES)
                    job = pool.submit(_check_members, parts, body, blk.indices, keys) if pool is not None else None
                    if job is None:
                        if not _check_members(parts, body, blk.indices, keys):
                            raise _block_error(ctx, blk)
                        if sort:
                            parts = _feed_pieces(_sorted_members(ctx, pieces or []))
                    if not first:
                        sink.write(feed(b","))
                    for part in parts:
                        sink.write(feed(part))
                    if job is not None and not job.result():
                        raise _block_error(ctx, blk)
                    first = False
                ctx.records += len(blk.indices)
                ctx.advance(blk)
            sink.write(feed(b"}"))
            sink.write(rf.finish())
            keys.report(ctx)
            ctx.commit_check()
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
    ctx.rows = ctx.records
    return [out]


def _export_jsonl(ctx: _Ctx, out: Path) -> list[Path]:
    rf = core.Reformatter(None)   # compact: a complete value leaves nothing pending
    with _output(out, ctx.gzip) as sink:
        _jsonl_records(ctx, rf.feed, sink)
        sink.write(rf.finish())
        ctx.commit_check()
    ctx.rows = ctx.records
    return [out]


# ----------------------------------------------------------------------------------
# several documents into one file (merge, docs/ARCHITECTURE.md §14.4)
# ----------------------------------------------------------------------------------
def _merged_warnings(ctxs: list[_Ctx]) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """The warnings of several contexts as one list ``(texts, keys, params)``: the counts of a
    key are added up, the other parameters are those of its first occurrence."""
    merged: dict[str, dict[str, Any]] = {}
    for ctx in ctxs:
        for key, entry in ctx._warnings.items():
            have = merged.get(key)
            if have is None:
                merged[key] = dict(entry)
            else:
                have["count"] = have.get("count", 0) + entry.get("count", 0)
    keys = list(merged)
    return [_tn(k, **merged[k]) for k in keys], keys, [dict(merged[k]) for k in keys]


def export_many(inputs: Sequence[tuple[Document, Any]], out_path: StrPath, opts: ExportOptions, *,
                progress: Progress | None = None, cancel: CancelToken | None = None,
                gzip: bool | None = None) -> ExportReport:
    """Write the records of several documents into ONE JSON or JSON Lines file (the merge of
    docs/ARCHITECTURE.md §14.4): ``inputs`` = ``[(document, source), ...]`` (``source`` as for
    :func:`export`: ``None`` = all records), records in input order, then in source order.

    * Everything goes through ONE ``core.Reformatter`` (``opts.indent``; JSON Lines: compact
      lines), so tokens are copied byte for byte -- exact numbers, no ``\\u`` escaping;
      ``sort_keys`` as for :func:`export`.
    * JSON root: one object of members when EVERY input has member records (keyed documents,
      wrapped documents with an object record container) -- member keys repeated across the
      records are written as they are and counted (``writer.warn.merge_duplicate_keys``) --,
      else an array, in which member records become ``{"key": value}`` objects
      (``writer.warn.members_as_objects``). JSON Lines: member records are ``{"key":value}``
      lines (as :func:`export`).
    * Wrapper (JSON, ``keep_wrapper``): the first input's wrapper is kept when it is a wrapped
      document whose record container is of the root's kind; any other wrapper is dropped
      (``writer.warn.wrappers_dropped``). JSON Lines has no place for one
      (``writer.note.wrapper_not_written``).
    * Huge records are streamed; partial inputs and broken JSON Lines lines are reported as by
      :func:`export` (the caller decides whether partial inputs are acceptable).
    * The file appears atomically (``gzip``: as for :func:`export`); every input is checked with
      ``ensure_unchanged()`` right before the commit. Progress: one bar over the records of all
      inputs. ``ExportReport.per_input``: the records written of each input."""
    if not isinstance(opts, ExportOptions):
        raise TypeError("opts must be an ExportOptions instance")
    opts.validate()
    if opts.fmt not in ("json", "jsonl"):
        raise UserInputError("writer.error.many_format", fmt=FORMAT_NAMES[opts.fmt])
    if opts.as_keyed:
        raise _invalid("as_keyed", opts.as_keyed, "writer.expected.as_keyed_many")
    if not inputs:
        raise UserInputError("writer.error.no_inputs")
    started = time.perf_counter()
    out = Path(os.path.abspath(os.fspath(out_path)))
    pinned: list[tuple[Document, Any]] = []
    for doc, source in inputs:
        d = _pin(doc, source)
        _check_target(d, out)
        pinned.append((d, source))
    gz = _check_gzip(opts, out, gzip)
    refuse_edited_targets([out])
    _purge_staging(pinned[0][0].settings)
    for d, _source in pinned:
        _ensure_index(d, progress, cancel)
    ctxs: list[_Ctx] = []
    for d, source in pinned:
        src, count = _normalize_source(d, source)
        ctx = _Ctx(d, opts, src, count, None, cancel, out)
        ctx.gzip = gz
        ctxs.append(ctx)
    try:
        base = 0
        for ctx in ctxs:                 # one bar: each input's meter reports from its own base
            ctx.meter.progress = progress
            ctx.meter.base = base
            base += ctx.meter.unit
        if progress is not None:
            progress.set_message(t("writer.progress.export", name=out.name))
            progress.set_total(max(1, base))
            progress.update(0)
            progress.restart_clock()
        for ctx in ctxs:
            ctx.note_source()
        top = ctxs[0]
        json_out = opts.fmt == "json"
        object_root = json_out and all(ctx.member for ctx in ctxs)
        if json_out and not object_root:
            members = sum(1 for ctx in ctxs if ctx.member)
            if members:
                top.warn("writer.warn.members_as_objects", add=members)
        wrapper = None
        if json_out and opts.keep_wrapper:
            dropped = [ctx for ctx in ctxs if ctx.doc.kind == "wrapped"]
            if top.doc.kind == "wrapped" and top.member == object_root:
                wrapper = _wrapper_spans(top)          # (a partial input: writer.warn.wrapper_dropped)
                dropped = dropped[1:]
            if dropped:
                top.warn("writer.warn.wrappers_dropped", add=len(dropped),
                         names=_names_text([ctx.doc.source_path.name for ctx in dropped]))
        rf = core.Reformatter(opts.indent if json_out else None)
        feed = rf.feed
        keys = _MemberKeys(top.doc) if object_root else None
        per_input: list[int] = []
        with _output(out, gz) as sink:
            if json_out:
                if wrapper is not None:
                    for chunk in top.chunks(*wrapper[0]):
                        sink.write(feed(chunk))
                sink.write(feed(b"{" if object_root else b"["))
                first = True
                written = 0
                for ctx in ctxs:
                    if keys is not None:
                        keys.offset = written
                    first = _json_records(ctx, feed, sink, first, wrap_members=ctx.member and not object_root,
                                          keys=keys)
                    per_input.append(ctx.records)
                    written += ctx.records
                sink.write(feed(b"}" if object_root else b"]"))
                if wrapper is not None:
                    for chunk in top.chunks(*wrapper[1]):
                        sink.write(feed(chunk))
            else:
                for ctx in ctxs:
                    _jsonl_records(ctx, feed, sink)
                    per_input.append(ctx.records)
            sink.write(rf.finish())
            if keys is not None:
                keys.report(top, "writer.warn.merge_duplicate_keys")
            check(cancel)
            for ctx in ctxs:
                ctx.doc.ensure_unchanged()
            refuse_edited_targets([out])         # an edit of the target may have begun meanwhile
    finally:
        for ctx in ctxs:
            ctx.close()
    for ctx in ctxs:
        ctx.finish()                     # (no progress of its own: the partial counts)
    if progress is not None:
        progress.finish()
    texts, wkeys, wparams = _merged_warnings(ctxs)
    try:
        size = os.stat(long_path(out)).st_size
    except OSError:  # pragma: no cover - just written
        size = 0
    records = sum(per_input)
    report = ExportReport(fmt=opts.fmt, records=records, rows=records, bytes_written=size, files=[out],
                          warnings=texts, elapsed=time.perf_counter() - started, warning_keys=wkeys,
                          per_input=per_input, warning_params=wparams)
    log.info("merged %d records of %d inputs into %s as %s: %d bytes in %.2f s%s", records, len(ctxs), out,
             opts.fmt, size, report.elapsed, f"; warnings: {', '.join(wkeys)}" if wkeys else "")
    return report


# ----------------------------------------------------------------------------------
# one node (the export dialog's "node" scope, CLI convert --node / extract --node)
# ----------------------------------------------------------------------------------
NODE_FORMATS = ("json", "jsonl")
NODE_READ = 1 * MiB               # node bytes read (and fed to the Reformatter) at a time
NODE_RANGE_MAX = 8 * MiB          # elements of a batch read with one read while their range is this small


def _reformat(data: bytes, indent: int | str | None) -> bytes:
    """One complete JSON value re-indented (token bytes unchanged)."""
    rf = core.Reformatter(indent)
    if len(data) <= NODE_READ:
        return rf.feed(data) + rf.finish()
    parts = [rf.feed(data[i:i + NODE_READ]) for i in range(0, len(data), NODE_READ)]
    parts.append(rf.finish())
    return b"".join(parts)


def _stream_value(src: Any, start: int, end: int, indent: int | str | None, out: Any,
                  cancel: CancelToken | None, on_read: Callable[[int], None] | None = None) -> None:
    """Re-indent the value at ``[start, end)`` of the open data reader ``src`` into ``out``,
    :data:`NODE_READ` bytes at a time."""
    rf = core.Reformatter(indent)
    pos = start
    src.seek(pos)
    while pos < end:
        check(cancel)
        chunk = src.read(min(NODE_READ, end - pos))
        if not chunk:
            raise UserInputError("writer.error.node_truncated")
        pos += len(chunk)
        out.write(rf.feed(chunk))
        if on_read is not None:
            on_read(pos)
    out.write(rf.finish())


def _stream_chunks(chunks: Iterable[bytes], indent: int | str | None, out: Any, cancel: CancelToken | None,
                   on_fed: Callable[[int], None] | None = None) -> None:
    """Re-indent one JSON value given in pieces (``doc.node_chunks``) into ``out``."""
    rf = core.Reformatter(indent)
    fed = 0
    for chunk in chunks:
        check(cancel)
        out.write(rf.feed(chunk))
        fed += len(chunk)
        if on_fed is not None:
            on_fed(fed)
    out.write(rf.finish())


def _write_record_lines(doc: Document, f: Any, cancel: CancelToken | None, on_fed: Callable[[int], None]) -> int:
    """JSON Lines of the records of an edited document (the array that holds them): every
    effective record compact on its own line, read record by record (file and edit layer)."""
    count = 0
    fed = 0
    for i, data in doc.iter_record_bytes(None, include_huge=True, cancel=cancel):
        if count % 256 == 0:
            check(cancel)
        if data is None:                   # a huge record: streamed from its own span
            start, end = doc.record_span(i)
            with doc.open_data() as src:
                _stream_value(src, start, end, None, f, cancel)
            fed += end - start
        else:
            f.write(_reformat(data, None))
            fed += len(data)
        f.write(b"\n")
        count += 1
        on_fed(fed)
    return count


def _write_child_lines(doc: Document, node: Any, f: Any, cancel: CancelToken | None,
                       on_fed: Callable[[int], None]) -> int:
    """JSON Lines of a composite array node that is not the record container (an ancestor of
    it): every element through ``doc.node_chunks`` (the one holding the records included)."""
    total = node.child_count(cancel=cancel)
    done = 0
    fed = 0
    while done < total:
        check(cancel)
        kids = node.children(done, min(total, done + NODE_BATCH))
        if not kids:
            break
        for kid in kids:
            _stream_chunks(doc.node_chunks(kid, chunk=NODE_READ, cancel=cancel), None, f, cancel)
            f.write(b"\n")
            fed += max(0, kid.size)
        done += len(kids)
        on_fed(fed)
    return done


def _write_lines(node: Any, src: Any, f: Any, cancel: CancelToken | None, on_read: Callable[[int], None]) -> int:
    """JSON Lines of an array node: every element compact on its own line."""
    count = node.child_count(cancel=cancel)
    done = 0
    while done < count:
        check(cancel)
        kids = node.children(done, min(count, done + NODE_BATCH))
        if not kids:
            break
        lo, hi = kids[0].start, kids[-1].end
        if hi - lo <= NODE_RANGE_MAX:
            src.seek(lo)
            buf = src.read(hi - lo)
            if len(buf) < hi - lo:
                raise UserInputError("writer.error.node_truncated")
            for k, kid in enumerate(kids):
                if k % 256 == 0:
                    check(cancel)
                f.write(_reformat(buf[kid.start - lo:kid.end - lo], None))
                f.write(b"\n")
        else:
            for kid in kids:
                _stream_value(src, kid.start, kid.end, None, f, cancel)
                f.write(b"\n")
        done += len(kids)
        on_read(kids[-1].end)
    return done


def export_node(doc: Document, node: Any, out_path: StrPath, fmt: str = "json", *, indent: int | str | None = 2,
                progress: Progress | None = None, cancel: CancelToken | None = None,
                gzip: bool | None = None) -> ExportReport:
    """Write one node of ``doc`` (``node``: a :class:`~jsonreshape.reader.Node` resolved on the
    document passed -- the GUI passes its pinned snapshot, so later edits do not leak in).

    ``fmt="json"``: the node's value re-indented with ``core.Reformatter(indent)`` from its raw
    bytes, streamed in 1 MiB pieces (numbers and strings byte-identical, no trailing newline --
    like :func:`export`). ``fmt="jsonl"``: one compact line per element when the node is an array,
    else the node on one line (``\\n`` after every line). The synthetic root of JSON Lines / stream
    / brace-list documents (no bytes of its own) is written with :func:`export` (all records). On
    an edited document the node's EFFECTIVE text is written: a composite node (the records'
    container or an ancestor of it) through ``doc.node_chunks``, an array of records record by
    record. Atomic; the source must be unchanged before the commit; ``gzip`` as for
    :func:`export`. Other formats: ``writer.error.node_format``."""
    fmt = fmt.strip().lower() if isinstance(fmt, str) else fmt
    if fmt not in NODE_FORMATS:
        raise UserInputError("writer.error.node_format", fmt=FORMAT_NAMES.get(str(fmt), str(fmt)))
    indent = _norm_indent(indent)
    started = time.perf_counter()
    pinned = getattr(doc, "pinned", None)
    if callable(pinned):
        doc = pinned()
    out = Path(os.path.abspath(os.fspath(out_path)))
    _check_target(doc, out)
    gz = gzip_path(out) if gzip is None else bool(gzip)
    if node.is_virtual:
        opts = ExportOptions(fmt, indent=indent if fmt == "json" else None)
        return export(doc, out, opts, None, progress=progress, cancel=cancel, gzip=gz)
    refuse_edited_targets([out])
    composite = bool(getattr(node, "is_composite", False)) and bool(getattr(doc, "edited", False))
    total = max(0, int(node.size)) if composite else max(0, node.end - node.start)
    if progress is not None:
        progress.set_message(t("writer.progress.node", name=out.name))
        progress.set_total(total)

    def on_read(pos: int) -> None:
        if progress is not None:
            progress.update(pos - node.start)

    def on_fed(done: int) -> None:
        if progress is not None:
            progress.update(min(done, total) if total else done)

    records = 1
    with _output(out, gz) as f:
        if composite:
            if fmt == "jsonl" and node.kind == "array":
                if getattr(node, "role", None) == "records":
                    records = _write_record_lines(doc, f, cancel, on_fed)
                else:
                    records = _write_child_lines(doc, node, f, cancel, on_fed)
            else:
                _stream_chunks(doc.node_chunks(node, chunk=NODE_READ, cancel=cancel),
                               indent if fmt == "json" else None, f, cancel, on_fed)
                if fmt == "jsonl":
                    f.write(b"\n")
        else:
            with doc.open_data() as src:
                if fmt == "json":
                    _stream_value(src, node.start, node.end, indent, f, cancel, on_read)
                elif node.kind != "array":
                    _stream_value(src, node.start, node.end, None, f, cancel, on_read)
                    f.write(b"\n")
                else:
                    records = _write_lines(node, src, f, cancel, on_read)
        check(cancel)
        doc.ensure_unchanged()
        refuse_edited_targets([out])     # an edit of the target may have begun meanwhile
    if progress is not None:
        progress.update(total)
        progress.finish()
    try:
        size = os.stat(long_path(out)).st_size
    except OSError:  # pragma: no cover - just written
        size = 0
    report = ExportReport(fmt=fmt, records=records, rows=records, bytes_written=size, files=[out],
                          elapsed=time.perf_counter() - started)
    log.info("exported node %s of %s to %s as %s: %d bytes", getattr(node, "path_text", "?"),
             doc.source_path.name, out, fmt, size)
    return report


# ----------------------------------------------------------------------------------
# CSV / XLSX: cells and rows
# ----------------------------------------------------------------------------------
_ORJSON_EXACT = orjson.OPT_PASSTHROUGH_SUBCLASS   # JsonFloat/JsonInt are refused, never rounded


def _json_text(v: Any) -> str:
    """Compact JSON of a container cell (``codec.dumps``; orjson directly when it can --
    it refuses exact-text numbers, big integers, lone surrogates and nesting deeper than
    255 levels, which go through ``codec.dumps``)."""
    try:
        return orjson.dumps(v, option=_ORJSON_EXACT).decode("utf-8")
    except TypeError:   # orjson.JSONEncodeError is a TypeError
        return codec.dumps(v).decode("utf-8")


class _TextCells:
    """CSV cell texts (see the module docstring)."""

    empty = ""

    def __init__(self, sep: str) -> None:
        number_text = codec.number_text
        dumps = orjson.dumps

        def one(v: Any) -> str:
            tv = type(v)
            if tv is str:
                return v
            if tv is int:
                return int.__repr__(v)
            if tv is float:
                return dumps(v).decode("ascii")
            if v is None or v is MISSING:
                return ""
            if tv is bool:
                return "true" if v else "false"
            if tv is list:
                return many(v)
            if tv is dict:
                return _json_text(v)
            return number_text(v)   # JsonFloat, JsonInt, Decimal: exact text

        def many(items: list[Any]) -> str:
            if items and type(items[0]) is str:
                try:
                    return sep.join(items)       # all strings (tags, names)
                except TypeError:
                    pass
            parts = []
            for e in items:
                te = type(e)
                if te is str:
                    parts.append(e)
                elif te is dict or te is list:
                    parts.append(_json_text(e))
                else:
                    parts.append(one(e))
            return sep.join(parts)

        def convert(row: list[Any]) -> list[Any]:
            # the csv module writes str as it is, int as str(int) (== its JSON text: plain
            # ints only, bool excluded by the type test) and None as ""; everything else
            # goes through one()
            return [x if (tx := type(x)) is str or tx is int or x is None
                    else dumps(x).decode("ascii") if tx is float else one(x) for x in row]

        self.one: Callable[[Any], Any] = one
        self.many: Callable[[list[Any]], Any] = many
        self.convert: Callable[[list[Any]], list[Any]] = convert

    @staticmethod
    def key(key: str | None) -> Any:
        return "" if key is None else key

    @staticmethod
    def index(index: int) -> Any:
        return str(index)


def _excel_number(text: str) -> float | None:
    """The number for an Excel cell when the JSON number ``text`` keeps its value there:
    at most 15 significant digits (trailing zeros do not count: ``1.10`` -> 1.1) and an
    exponent Excel can hold; ``None`` otherwise."""
    try:
        d = Decimal(text)
    except InvalidOperation:  # pragma: no cover - codec number texts are valid
        return None
    if not d.is_finite():  # pragma: no cover
        return None
    if d.is_zero():
        return float(d)
    digits = d.as_tuple().digits
    n = len(digits)
    while n > 1 and digits[n - 1] == 0:
        n -= 1
    if n > XLSX_DIGITS or not -XLSX_MAX_EXPONENT <= d.adjusted() <= XLSX_MAX_EXPONENT:
        return None
    return float(d)


class _XlsxCells:
    """Typed XLSX cell values: ``None`` (blank), ``str``, ``bool``, ``int``/``float``."""

    empty = None

    def __init__(self, sep: str) -> None:
        text = _TextCells(sep)
        text_one, text_many = text.one, text.many
        number_text = codec.number_text
        self.numbers_as_text = 0

        def number(v: Any) -> Any:
            txt = number_text(v)
            val = _excel_number(txt)
            if val is None:
                self.numbers_as_text += 1
                return txt
            return val

        def one(v: Any) -> Any:
            tv = type(v)
            if tv is str:
                return v
            if tv is int:
                if -_EXACT_INT < v < _EXACT_INT:
                    return v
                return number(v)
            if tv is float:
                return number(v)
            if v is None or v is MISSING:
                return None
            if tv is bool:
                return v
            if tv is list or tv is dict:
                return text_one(v)
            return number(v)

        def many(items: list[Any]) -> Any:
            if len(items) == 1:
                v = items[0]
                if type(v) is not list and type(v) is not dict:
                    return one(v)
            s = text_many(items)
            return s if s else None

        lo, hi = -_EXACT_INT, _EXACT_INT

        def convert(row: list[Any]) -> list[Any]:
            return [x if (tx := type(x)) is str or x is None or (tx is int and lo < x < hi) else one(x) for x in row]

        self.one: Callable[[Any], Any] = one
        self.many: Callable[[list[Any]], Any] = many
        self.convert: Callable[[list[Any]], list[Any]] = convert

    @staticmethod
    def key(key: str | None) -> Any:
        return key

    @staticmethod
    def index(index: int) -> Any:
        return index


def _getter(segs: tuple) -> Callable[[Any], Any]:
    """Fast concrete-path lookup (``paths.get`` semantics; ``MISSING`` when absent)."""
    if not segs:
        return lambda v: v
    if all(isinstance(s, str) and canonical_index(s) is None for s in segs):
        # object keys only: a list, scalar or missing value on the way means missing
        if len(segs) == 1:
            k0 = segs[0]
            return lambda v: v.get(k0, MISSING) if type(v) is dict else MISSING

        def g(v: Any) -> Any:
            for k in segs:
                if type(v) is not dict:
                    return MISSING
                v = v.get(k, MISSING)
            return v

        return g
    return lambda v: get(v, segs)


def _value_fn(segs: tuple, cells: Any) -> Callable[[Any], Any]:
    """value -> cell for a column path (concrete: one value; wildcard: all values)."""
    one, many = cells.one, cells.many
    if any(isinstance(s, Wild) for s in segs):
        values = PathPattern(segs).values
        return lambda v: many(values(v))
    if not segs:
        return one
    if all(isinstance(s, str) and canonical_index(s) is None for s in segs):
        if len(segs) == 1:
            k0 = segs[0]

            def f1(v: Any) -> Any:
                return one(v.get(k0, MISSING) if type(v) is dict else MISSING)

            return f1
        if len(segs) == 2:
            k0, k1 = segs

            def f2(v: Any) -> Any:
                if type(v) is dict:
                    v = v.get(k0, MISSING)
                    if type(v) is dict:
                        return one(v.get(k1, MISSING))
                return one(MISSING)

            return f2
    getter = _getter(segs)
    return lambda v: one(getter(v))


def _cell_from_values(cells: Any, pattern: PathPattern, values: list[Any]) -> Any:
    if pattern.is_concrete:
        return cells.one(values[0] if values else MISSING)
    return cells.many(values)


def _simple_path(segs: tuple) -> bool:
    """Object keys only (no wildcard, no index, no key that would index a list), at most
    :data:`MAX_DEPTH` deep (deeper columns are looked up iteratively)."""
    return bool(segs) and len(segs) <= MAX_DEPTH and all(isinstance(s, str) and canonical_index(s) is None
                                                          for s in segs)


def _key_tree(columns: list[_Column]) -> tuple | None:
    """``(leaves, groups)``: ``leaves`` = ``((key, column position), ...)`` of the simple
    columns ending at this level, ``groups`` = ``((key, subtree), ...)`` (columns sharing
    a prefix look it up once). Simple paths are at most MAX_DEPTH deep, so the recursion
    here and in :func:`_fill_tree` is bounded."""
    root: dict[str, Any] = {"leaves": [], "groups": {}}
    found = False
    for pos, col in enumerate(columns):
        if col.special is None and _simple_path(col.segments):
            found = True
            node = root
            for seg in col.segments[:-1]:
                node = node["groups"].setdefault(seg, {"leaves": [], "groups": {}})
            node["leaves"].append((col.segments[-1], pos))

    def freeze(node: dict[str, Any]) -> tuple:
        return tuple(node["leaves"]), tuple((k, freeze(sub)) for k, sub in node["groups"].items())

    return freeze(root) if found else None


def _fill_tree(tree: tuple, obj: dict, row: list[Any]) -> None:
    """Raw values of the simple columns (``MISSING`` stays where nothing is found)."""
    leaves, groups = tree
    get = obj.get
    for k, pos in leaves:
        row[pos] = get(k, MISSING)
    for k, sub in groups:
        x = get(k)
        if type(x) is dict:
            _fill_tree(sub, x, row)


def _pool_fn(segs: tuple, keep: frozenset, cells: Any) -> Callable[[Any], Any]:
    """value -> cell of a map's pooled column: the members of the object at ``segs`` whose
    keys have no column of their own, as one compact JSON object (empty when none)."""
    getter = _getter(segs)
    empty = cells.empty

    def pool(v: Any) -> Any:
        obj = getter(v)
        if type(obj) is not dict:
            return empty
        rest = {k: x for k, x in obj.items() if k not in keep}
        return _json_text(rest) if rest else empty

    return pool


def _plain_fn(full: Callable[[Any], Any], get_explode: Callable[[Any], Any], empty: Any) -> Callable[[Any], Any]:
    """value -> cell of the automatic plain column at the expanded path (rows mode): the
    value there when it is not an array (the elements have their own columns)."""
    return lambda v: empty if type(get_explode(v)) is list else full(v)


def _probe_fn(pattern: PathPattern) -> Callable[[Any], bool]:
    """value -> whether ``pattern`` finds a value in it (null counts)."""
    if pattern.is_concrete:
        getter = _getter(pattern.segments)
        return lambda v: getter(v) is not MISSING
    values = pattern.values
    return lambda v: bool(values(v))


class _RowBuilder:
    """Records -> rows of cells for the CSV/XLSX columns (join or rows mode).

    ``track``: explicit columns are watched until they find a value (``unfound``: the
    positions of those that never did, ``writer.warn.columns_not_found``)."""

    def __init__(self, columns: list[_Column], cells: Any, explode: tuple | None, sep: str = ";",
                 track: bool = False) -> None:
        self.cells = cells
        self.sep = sep
        self.width = len(columns)
        self.texts = [c.text for c in columns]
        empty = cells.empty
        self.full: list[Callable[[Any], Any]] = []
        self.specials: list[tuple[int, str]] = []
        self.patterns: list[tuple[int, PathPattern]] = []
        for pos, col in enumerate(columns):
            if col.special is not None:
                self.specials.append((pos, col.special))
                self.full.append(lambda v, _e=empty: _e)
            else:
                assert col.pattern is not None
                if col.keep is not None:
                    self.full.append(_pool_fn(col.segments[:-1], col.keep, cells))
                else:
                    self.full.append(_value_fn(col.segments, cells))
                self.patterns.append((pos, col.pattern))
        # join mode: simple columns come from one pass over a key tree; the others from
        # their own (converted) functions; then one conversion pass (idempotent on cells)
        self._tree = _key_tree(columns)
        self._complex = [(pos, self.full[pos]) for pos, col in enumerate(columns)
                         if col.special is None and not _simple_path(col.segments)]
        self._blank = [MISSING] * self.width
        self.explode = explode
        self.outer: list[tuple[int, Callable[[Any], Any]]] = []
        self.inner: list[tuple[int, Callable[[Any], Any]]] = []
        self.outer_patterns: list[tuple[int, PathPattern]] = []
        self.inner_patterns: list[tuple[int, PathPattern]] = []
        if explode is not None:
            get_explode = self.get_explode = _getter(explode)
            n = len(explode)
            for pos, pattern in self.patterns:
                col = columns[pos]
                segs = pattern.segments
                if segs[:n] == explode and (len(segs) == n or segs[n] is Wild.ANY_INDEX) and not col.plain:
                    rest = segs[n + 1:]
                    if col.keep is not None:
                        self.inner.append((pos, _pool_fn(rest[:-1], col.keep, cells)))
                    else:
                        self.inner.append((pos, _value_fn(rest, cells)))
                    self.inner_patterns.append((pos, PathPattern(rest)))
                else:
                    fn = self.full[pos]
                    if col.plain:
                        fn = _plain_fn(fn, get_explode, empty)
                    self.outer.append((pos, fn))
                    self.outer_patterns.append((pos, pattern))
        self.unfound: dict[int, Callable[[Any], bool]] = {}
        if track:
            for pos, pattern in self.patterns:
                if pattern.segments:          # "$" (the record) is always found
                    self.unfound[pos] = _probe_fn(pattern)
        self._inner_probes = {pos: _probe_fn(p) for pos, p in self.inner_patterns if pos in self.unfound}

    def _set_specials(self, row: list[Any], key: str | None, index: int) -> None:
        cells = self.cells
        for pos, kind in self.specials:
            row[pos] = cells.key(key) if kind == "key" else cells.index(index)

    def _probe(self, value: Any) -> None:
        """Explicit columns that find a value in the record ``value`` are found."""
        found = [pos for pos, probe in self.unfound.items() if probe(value)]
        for pos in found:
            del self.unfound[pos]

    def _probe_item(self, item: Any) -> None:
        """... in an element of the expanded array (its inner columns)."""
        for pos, probe in self._inner_probes.items():
            if pos in self.unfound and probe(item):
                del self.unfound[pos]

    def join_row(self, value: Any, key: str | None, index: int) -> list[Any]:
        """The row of a record in join mode (fast path)."""
        if self.unfound:
            self._probe(value)
        row = self._blank.copy()
        if self._tree is not None and type(value) is dict:
            _fill_tree(self._tree, value, row)
        for pos, fn in self._complex:
            row[pos] = fn(value)
        row = self.cells.convert(row)
        if self.specials:
            self._set_specials(row, key, index)
        return row

    def rows(self, value: Any, key: str | None, index: int) -> list[list[Any]]:
        """The rows of a record: one in join mode; in rows mode one per element of the
        expanded array (a missing/empty/non-array value gives one row)."""
        if self.explode is None:
            return [self.join_row(value, key, index)]
        if self.unfound:
            self._probe(value)
        arr = self.get_explode(value)
        if type(arr) is list and arr:
            base = [self.cells.empty] * self.width
            for pos, fn in self.outer:
                base[pos] = fn(value)
            if self.specials:
                self._set_specials(base, key, index)
            out = []
            inner = self.inner
            for item in arr:
                row = base.copy()
                for pos, fn in inner:
                    row[pos] = fn(item)
                out.append(row)
            return out
        row = [fn(value) for fn in self.full]
        if self.specials:
            self._set_specials(row, key, index)
        return [row]

    # -- huge records and records too deep for codec.loads (explicit columns) ---------
    def _explode_node(self, ctx: _Ctx, index: int) -> Any:
        assert self.explode is not None
        node = ctx.doc.record_node(index)
        for seg in self.explode:
            node = node.find_child(seg, cancel=ctx.cancel)
            if node is None:
                return None
        if node.kind != "array" or node.child_count(cancel=ctx.cancel) == 0:
            return None
        return node

    def _stream_into(self, ctx: _Ctx, row: list[Any], cols: list[tuple[int, PathPattern]], span: tuple[int, int],
                     member: bool, index: int) -> None:
        """The cells of ``cols`` (position, pattern) of a record (or of one element) read
        with ``stream_values`` within the capture budget; when the values need more, text
        cells built from the stream (:func:`_stream_cells`) of at most ``ctx.text_cap``
        characters -- a longer cell is cut there (``writer.warn.cell_truncated``), the
        other cells of the row are complete."""
        if not cols:
            return
        cells = self.cells
        pats = [p for _, p in cols]
        vals: dict[int, list[Any]] | None = None
        try:
            vals = ctx.stream_values(span, pats, member=member)
        except _capture_error():
            pass        # (left first: the traceback would keep the first pass's values alive)
        if vals is None:
            cap = ctx.text_cap(len(pats))
            built = _stream_cells(ctx, span, pats, member, self.sep, cap)
            for (pos, _), sc in zip(cols, built):
                row[pos] = sc.cell(cells)
                if sc.matched:
                    self.unfound.pop(pos, None)
                if sc.truncated:
                    ctx.warn("writer.warn.cell_truncated", first={"index": index, "column": self.texts[pos]},
                             limit=cap)
            return
        for j, (pos, pattern) in enumerate(cols):
            row[pos] = _cell_from_values(cells, pattern, vals[j])
            if vals[j]:
                self.unfound.pop(pos, None)

    def streamed_rows(self, ctx: _Ctx, index: int, span: tuple[int, int]) -> Iterator[list[list[Any]]]:
        """Rows of a record read with ``stream_values`` (never decoded whole); rows mode
        walks the expanded array through the node index (one element in memory at a
        time). Values beyond the capture budget give text cells (:meth:`_stream_into`)."""
        doc, cells = ctx.doc, self.cells
        member = ctx.member
        key = doc.record_key(index) if member else None
        node = self._explode_node(ctx, index) if self.explode is not None else None
        if node is not None:
            base = [cells.empty] * self.width
            self._stream_into(ctx, base, self.outer_patterns, span, member, index)
            if self.specials:
                self._set_specials(base, key, index)
            count = node.child_count(cancel=ctx.cancel)
            limit = ctx.batch_rows(self.width)
            for start in range(0, count, NODE_BATCH):
                check(ctx.cancel)
                batch = []
                for child in node.children(start, min(count, start + NODE_BATCH)):
                    row = base.copy()
                    if child.is_large:
                        self._stream_into(ctx, row, self.inner_patterns, (child.start, child.end), False, index)
                    else:
                        item = child.value()
                        if self.unfound:
                            self._probe_item(item)
                        for pos, fn in self.inner:
                            row[pos] = fn(item)
                    batch.append(row)
                    if len(batch) >= limit:
                        yield batch
                        batch = []
                if batch:
                    yield batch
            ctx.records += 1
            return
        row = [cells.empty] * self.width
        self._stream_into(ctx, row, self.patterns, span, member, index)
        if self.specials:
            self._set_specials(row, key, index)
        ctx.records += 1
        yield [row]


# ----------------------------------------------------------------------------------
# cells of values beyond the capture budget, built from the ijson events
# ----------------------------------------------------------------------------------
_NOTHING = object()
_NO_STATES: frozenset = frozenset()
_UINT64_MAX = 2**64 - 1


def _seg_matches(pseg: Any, seg: Any) -> bool:
    """A pattern segment against a path segment (``PathPattern.matches`` semantics)."""
    if pseg is Wild.ANY_INDEX:
        return type(seg) is int
    if pseg is Wild.ANY_KEY:
        return type(seg) is str
    if type(pseg) is int:
        return type(seg) is int and seg == pseg
    if type(seg) is int:
        return canonical_index(pseg) == seg
    return seg == pseg


class _Nfa:
    """The pattern positions reached along a path (as ``PathPattern.resolve``: ``**``
    matches zero or more segments); steps are cached (array positions the pattern does
    not name share one entry; at most 65536 entries)."""

    __slots__ = ("segs", "n", "_closures", "_steps", "_ints")

    def __init__(self, segs: tuple) -> None:
        self.segs = segs
        self.n = len(segs)
        self._closures: dict[frozenset, frozenset] = {}
        self._steps: dict[tuple, frozenset] = {}
        ints: set[int] = set()
        for s in segs:
            if type(s) is int:
                ints.add(s)
            elif type(s) is str:
                ci = canonical_index(s)
                if ci is not None:
                    ints.add(ci)
        self._ints = ints

    def closure(self, states: frozenset) -> frozenset:
        c = self._closures.get(states)
        if c is None:
            out = set(states)
            for i in states:
                while i < self.n and self.segs[i] is Wild.DESCENDANTS:
                    i += 1
                    out.add(i)
            c = self._closures[states] = frozenset(out)
        return c

    def initial(self) -> frozenset:
        return self.closure(frozenset((0,)))

    def step(self, states: frozenset, seg: Any) -> frozenset:
        key = (states, seg if type(seg) is not int or seg in self._ints else -1)
        res = self._steps.get(key)
        if res is None:
            nxt = set()
            for i in states:
                if i < self.n:
                    p = self.segs[i]
                    if p is Wild.DESCENDANTS:
                        nxt.add(i)
                    elif _seg_matches(p, seg):
                        nxt.add(i + 1)
            res = self.closure(frozenset(nxt)) if nxt else _NO_STATES
            if len(self._steps) >= 65536:
                self._steps.clear()
            self._steps[key] = res
        return res


def _stream_number(v: Any) -> Any:
    """An ijson number -> the type ``codec.loads`` gives the same token."""
    if type(v) is int and _INT64_MIN <= v <= _UINT64_MAX:
        return v
    return codec.loads(str(v))


def _raw_text(event: str, value: Any) -> str:
    """A scalar's text as one of the joined values of a cell (``_TextCells``)."""
    if event == "string":
        return value
    if event == "number":
        return codec.number_text(_stream_number(value))
    if event == "boolean":
        return "true" if value else "false"
    return ""


def _json_token(event: str, value: Any) -> str:
    """A scalar's compact JSON text."""
    if event == "string":
        return _json_text(value)
    if event == "number":
        return codec.number_text(_stream_number(value))
    if event == "boolean":
        return "true" if value else "false"
    return "null"


class _StreamCell:
    """The text of one cell built from streamed values (:func:`_stream_cells`), at most
    ``cap`` characters: a longer text is cut there (``truncated``)."""

    __slots__ = ("concrete", "sep", "cap", "chunks", "pieces", "size", "parts", "truncated", "only", "nested",
                 "capturing", "matched", "text_only")

    def __init__(self, concrete: bool, sep: str, cap: int) -> None:
        self.concrete = concrete
        self.sep = sep
        self.cap = cap
        self.matched = False        # the pattern matched some value (null included)
        self.reset()

    def reset(self) -> None:
        """Forget the text so far (a concrete column takes the last match)."""
        self.chunks: list[str] = []  # the text: joined pieces ...
        self.pieces: list[str] = []  # ... and the pieces since
        self.size = 0
        self.parts = 0
        self.truncated = False
        self.only: Any = _NOTHING   # the value when the cell is one scalar (typed XLSX cell)
        self.nested: list[Any] = []  # matches inside the captured match (``**``): [start, end] or text
        self.capturing = 0           # open captures of this cell's matches
        self.text_only = False       # a concrete column found a container: a text cell, even if empty

    def room(self) -> int:
        return self.cap - self.size

    def add(self, s: str) -> None:
        if not s:
            return
        room = self.cap - self.size
        if len(s) > room:
            if room <= 0:
                self.truncated = True
                return
            s = s[:room]
            self.truncated = True
        pieces = self.pieces
        pieces.append(s)
        self.size += len(s)
        if len(pieces) >= 4096:          # millions of tiny strings would cost ~50 bytes each
            self.chunks.append("".join(pieces))
            pieces.clear()

    def text(self) -> str:
        if self.chunks:
            return "".join(self.chunks) + "".join(self.pieces)
        return "".join(self.pieces)

    def part(self) -> None:
        """The next of the joined values starts."""
        if self.parts:
            self.add(self.sep)
        self.parts += 1

    def end_capture(self) -> None:
        """The captured match ended: the matches inside it follow as values of their own."""
        nested, self.nested = self.nested, []
        if not nested or self.truncated:
            if nested:
                self.truncated = True
            return
        text = self.text() if any(type(x) is list for x in nested) else ""
        for x in nested:
            self.part()
            self.add(text[x[0]:x[1]] if type(x) is list else x)

    def cell(self, cells: Any) -> Any:
        if self.parts == 1 and self.only is not _NOTHING and not self.truncated:
            return cells.one(self.only)
        text = self.text()
        if not text and cells.empty is None and not self.text_only:
            return None
        return text


def _cut_to(s: str, room: int) -> str:
    """``s``, or its first ``room + 1`` characters (enough to know it does not fit)."""
    return s if len(s) <= room else s[:max(0, room) + 1]


def _stream_cells(ctx: _Ctx, span: tuple[int, int], patterns: list[PathPattern], member: bool, sep: str,
                  cap: int) -> list[_StreamCell]:
    """The cells of ``patterns`` in a record (``member``: an object member) or an element,
    read event by event (ijson) and kept as text only, at most ``cap`` characters each.
    The texts are those of ``_TextCells.one``/``many`` for the same values (a concrete
    array: its elements joined; containers: compact JSON; XLSX: a single scalar keeps its
    type); deviations: those of :mod:`jsonreshape.stream` (ijson), and duplicate keys are
    not merged (a concrete column takes the last match, a wildcard column every match)."""
    n = len(patterns)
    rng = range(n)
    nfas = [_Nfa(p.segments) for p in patterns]
    finals = [f.n for f in nfas]
    out = [_StreamCell(p.is_concrete, sep, cap) for p in patterns]
    caps: list[list[Any]] = []      # [cell, depth]: compact JSON of the containers being captured
    joins: list[list[Any]] = []     # [cell, depth]: a concrete array whose elements are joined
    inner: list[list[Any]] = []     # [entry of cell.nested, cell, depth]: containers matched inside
    stack: list[list[Any]] = []     # [is_map, key, children so far, pattern states or None]
    root = [f.initial() for f in nfas]
    started = not member            # a member span starts with '{' and the key
    events = iter_events(ctx.doc, span, member)
    count = 0
    try:
        for ev, val in events:
            count += 1
            if count & (STREAM_CHECK_EVENTS - 1) == 0:
                check(ctx.cancel)
            if not started:
                started = ev == "map_key"
                continue
            if ev == "map_key":
                stack[-1][1] = val
                continue
            if ev == "end_map" or ev == "end_array":
                if not stack:
                    break                       # the '}' around a member
                d = len(stack)
                if caps:
                    close = "}" if ev == "end_map" else "]"
                    for c in caps:
                        c[0].add(close)
                while inner and inner[-1][2] == d:
                    entry, cell, _ = inner.pop()
                    entry[1] = cell.size
                while caps and caps[-1][1] == d:
                    cell = caps.pop()[0]
                    cell.capturing -= 1
                    if not cell.capturing:
                        cell.end_capture()
                while joins and joins[-1][1] == d:
                    joins.pop()
                stack.pop()
                continue
            # a value starts
            d = len(stack)
            container = ev == "start_map" or ev == "start_array"
            tok = "{" if ev == "start_map" else "[" if container else ""
            if d:
                top = stack[-1]
                is_map = top[0]
                seg = top[1] if is_map else top[2]
                first = top[2] == 0
                top[2] += 1
                pst = top[3]
                states: list[frozenset] | None = None
                if pst is not None:
                    states = [nfas[k].step(pst[k], seg) if pst[k] else _NO_STATES for k in rng]
                    if not any(states):
                        states = None
            else:
                top, is_map, first, states = None, False, True, root
            if caps:
                text = None
                for c in caps:
                    cell = c[0]
                    if cell.truncated:
                        continue
                    if text is None:
                        if not container:
                            if ev == "string":
                                tok = _json_text(_cut_to(val, max(x[0].room() for x in caps)))
                            else:
                                tok = _json_token(ev, val)
                        text = ("" if first else ",") + (_json_text(top[1]) + ":" if is_map else "") + tok
                    cell.add(text)
            if joins and joins[-1][1] == d:     # an element of joined arrays
                for j in joins:
                    if j[1] != d:
                        continue
                    cell = j[0]
                    cell.part()
                    if container:
                        cell.add(tok)
                        caps.append([cell, d + 1])
                        cell.capturing += 1
                    else:
                        cell.add(_raw_text(ev, _cut_to(val, cell.room()) if ev == "string" else val))
            if states is not None:
                for k in rng:
                    if finals[k] not in states[k]:
                        continue
                    cell = out[k]
                    cell.matched = True
                    if cell.capturing:          # inside a container this cell captures (``**``)
                        if not cell.truncated:
                            if container:
                                entry = [cell.size - len(tok), None]
                                cell.nested.append(entry)
                                inner.append([entry, cell, d + 1])
                            else:
                                cell.nested.append(_raw_text(ev, _cut_to(val, cell.cap) if ev == "string" else val))
                        continue
                    if cell.concrete:
                        cell.reset()
                        cell.text_only = container
                    if container:
                        if cell.concrete and ev == "start_array":
                            joins.append([cell, d + 1])
                        else:
                            cell.part()
                            cell.add(tok)
                            caps.append([cell, d + 1])
                            cell.capturing += 1
                    else:
                        cell.part()
                        cell.add(_raw_text(ev, _cut_to(val, cell.room()) if ev == "string" else val))
                        if cell.parts == 1 and not cell.truncated:
                            cell.only = _stream_number(val) if ev == "number" else val
            if container:
                stack.append([ev == "start_map", None, 0, states])
    finally:
        events.close()
    return out


def _explode_from_columns(columns: list[_Column]) -> tuple | None:
    """The concrete path before the first ``[*]`` of the first column that has one."""
    for col in columns:
        if col.pattern is None:
            continue
        for i, seg in enumerate(col.segments):
            if seg is Wild.ANY_INDEX:
                return col.segments[:i]
            if isinstance(seg, Wild):
                break
    return None


def _infer_explode(ctx: _Ctx, columns: list[_Column]) -> tuple | None:
    """Explicit columns in rows mode without ``[*]``: the first concrete column whose
    values include an array (records are scanned until the first column has one)."""
    cands = [(pos, _getter(col.segments)) for pos, col in enumerate(columns)
             if col.pattern is not None and col.pattern.is_concrete]
    best: int | None = None
    if cands:
        ctx.message("writer.progress.columns")
        member, split = ctx.member, ctx.doc.split_member
        first = cands[0][0]
        for blk in ctx.blocks():
            if not blk.huge:
                sp = _pairs(blk.spans)
                data = blk.data
                for k in range(len(blk.indices)):
                    if k & (CHECK_EVERY - 1) == CHECK_EVERY - 1:
                        check(ctx.cancel)
                    raw = data[sp[2 * k]:sp[2 * k + 1]]
                    if member:
                        raw = raw[split(raw)[1]:]
                    try:
                        value = _scan_loads(raw)
                    except _DECODE_ERRORS:
                        continue
                    for pos, g in cands:
                        if best is not None and pos >= best:
                            break
                        if type(g(value)) is list:
                            best = pos
                            break
                    if best == first:
                        break
            ctx.advance(blk)
            if best == first:
                break
    ctx.next_pass()
    ctx.message("writer.progress.export")
    return None if best is None else columns[best].segments


def _names_text(names: list[str], shown: int = 5) -> str:
    """The first ``shown`` names, then ``…``."""
    return ", ".join(names[:shown] + (["…"] if len(names) > shown else []))


def _prepare_columns(ctx: _Ctx) -> None:
    opts = ctx.opts
    rows = opts.array_mode == "rows"
    explode = tuple(parse_path(opts.explode)) if opts.explode is not None else None
    if opts.columns is not None:
        columns = [_parse_column(c) for c in opts.columns]
        if rows and explode is None:
            explode = _explode_from_columns(columns)
            if explode is None:
                explode = _infer_explode(ctx, columns)
    else:
        ctx.message("writer.progress.columns")
        scan = _discover(ctx.doc, ctx.src, ctx.count, None, rows, explode, ctx.cancel, ctx.advance,
                         workers=_pool_workers(ctx))
        ctx.workers = max(ctx.workers, scan.workers)
        ctx.next_pass()
        ctx.message("writer.progress.export")
        paths, found = scan.columns()
        if rows and explode is None:
            explode = found
        plans = scan.plans()
        pools = {pl.pool: pl.kept for pl in plans.values() if pl.pool is not None and pl.kept}
        plain = explode if rows else None
        columns = [_Column(KEY_COLUMN, special="key")] if ctx.member else []
        columns.extend(_path_column(p, keep=pools.get(p), plain=p == plain) for p in paths)
        final = set(paths)
        pooled = [pl for pl in plans.values() if pl.pool is not None and pl.pool in final]
        if pooled:
            ctx.warn("writer.warn.map_columns", add=len(pooled), limit=MAP_MIN_KEYS, max=MAP_KEEP_MAX,
                     paths=_names_text([_pool_text(pl) for pl in pooled]))
        deep = [p for p in scan.deep if p in final]
        if deep:
            ctx.warn("writer.warn.deep_columns", add=len(deep), limit=scan.depth)
        if scan.capped is not None:
            ctx.warn("writer.warn.columns_capped", add=len(paths), max=MAX_AUTO_COLUMNS, depth=scan.capped)
    if not columns and ctx.count:
        raise UserInputError("writer.error.no_columns")
    ctx.columns = columns
    ctx.explode = explode if rows else None


def _iter_row_batches(ctx: _Ctx, builder: _RowBuilder) -> Iterator[list[list[Any]]]:
    """Batches of rows for the selection (at most ``ctx.batch_rows`` rows each). Huge
    records and records too deep for ``codec.loads``: streamed with explicit columns,
    skipped with automatic ones."""
    explicit = ctx.opts.columns is not None
    doc, cancel = ctx.doc, ctx.cancel
    rows = builder.rows
    join_row = builder.join_row
    loads = codec.loads
    member = ctx.member
    split = doc.split_member
    fast = builder.explode is None and not member
    limit = ctx.batch_rows(builder.width)
    # fast path (one row per record): check the batch size every `step` records
    step = 1
    while step * 2 <= min(limit, CHECK_EVERY):
        step *= 2
    mask = step - 1
    for blk in ctx.blocks():
        if blk.huge:
            index = int(blk.indices[0])
            if explicit:
                yield from builder.streamed_rows(ctx, index, blk.span(0))
            else:
                ctx.skip_huge("writer.warn.huge_skipped")
            ctx.advance(blk)
            continue
        out: list[list[Any]] = []
        sp = _pairs(blk.spans)
        data = blk.data
        idx = blk.indices
        k = 0
        n = len(idx)
        while k < n:
            # the hot loop of table exports; a decoding failure leaves it for one record
            try:
                if fast:
                    append = out.append
                    while k < n:
                        if k & mask == mask:
                            if k & (CHECK_EVERY - 1) == CHECK_EVERY - 1:
                                check(cancel)
                            if len(out) >= limit:
                                yield out
                                out = []
                                append = out.append
                        append(join_row(loads(data[sp[2 * k]:sp[2 * k + 1]]), None, idx[k]))
                        k += 1
                else:
                    while k < n:
                        if k & (CHECK_EVERY - 1) == CHECK_EVERY - 1:
                            check(cancel)
                        if len(out) >= limit:
                            yield out
                            out = []
                        key, value = _decode(data[sp[2 * k]:sp[2 * k + 1]], member, split)
                        out.extend(rows(value, key, idx[k]))
                        k += 1
            except _DECODE_ERRORS:
                index = int(idx[k])
                k += 1
                if out:
                    yield out
                    out = []
                if explicit:
                    yield from builder.streamed_rows(ctx, index, doc.record_span(index))
                    ctx.records -= 1     # counted with the block below
                else:
                    ctx.skip_deep("writer.warn.deep_skipped")
                    ctx.records -= 1
        ctx.records += len(idx)
        if out:
            yield out
        ctx.advance(blk)


# ----------------------------------------------------------------------------------
# CSV
# ----------------------------------------------------------------------------------
def _flush_text(buf: io.StringIO, sink: _Sink) -> None:
    data = buf.getvalue()
    if data:
        sink.write(data.encode("utf-8", "backslashreplace"))
        buf.seek(0)
        buf.truncate(0)


def _builder(ctx: _Ctx, cells: Any) -> _RowBuilder:
    return _RowBuilder(ctx.columns, cells, ctx.explode, ctx.opts.join_sep, track=ctx.opts.columns is not None)


def _warn_unfound(ctx: _Ctx, builder: _RowBuilder) -> None:
    """Explicit columns that found a value in no exported record (a misspelled path?)."""
    if builder.unfound and ctx.records:
        names = [builder.texts[pos] for pos in sorted(builder.unfound)]
        # ``names``: every column, for callers combining reports (ExportReport.warning_params)
        ctx.warn("writer.warn.columns_not_found", add=len(names), columns=_names_text(names, 10), names=names)


def _csv_writer(buf: io.StringIO, delimiter: str) -> Any:
    """The :mod:`csv` writer of every CSV export (minimal quoting, ``\\r\\n`` line ends)."""
    return csv.writer(buf, delimiter=delimiter, quotechar='"', doublequote=True, quoting=csv.QUOTE_MINIMAL,
                      lineterminator="\r\n")


def _write_rows(csv_writer: Any, rows: list[list[Any]]) -> None:
    for i in range(0, len(rows), CSV_ROWS_PER_CALL):
        csv_writer.writerows(rows[i:i + CSV_ROWS_PER_CALL])


def _export_csv(ctx: _Ctx, out: Path) -> list[Path]:
    opts = ctx.opts
    builder = _builder(ctx, _TextCells(opts.join_sep))
    with _output(out, ctx.gzip) as sink:
        if opts.csv_bom:
            sink.write(codecs.BOM_UTF8)
        buf = io.StringIO()
        csv_writer = _csv_writer(buf, opts.csv_delimiter)
        if ctx.columns:
            csv_writer.writerow([c.text for c in ctx.columns])
        workers = _pool_workers(ctx)
        if workers > 1:
            _RowsPool(ctx, builder, _CsvOut(csv_writer, buf, sink), workers).run()
        else:
            for batch in _iter_row_batches(ctx, builder):
                _write_rows(csv_writer, batch)
                ctx.rows += len(batch)
                if buf.tell() >= CSV_FLUSH_CHARS:
                    _flush_text(buf, sink)
        _flush_text(buf, sink)
        ctx.commit_check()
    _warn_unfound(ctx, builder)
    return [out]


# ----------------------------------------------------------------------------------
# worker processes: the CSV rows and the column scan (module docstring, "CSV and XLSX")
# ----------------------------------------------------------------------------------
_POOL_STATE: dict[str, Any] = {}       # a worker process: its job ("rows" / "scan")


def _auto_workers(want: int | None, count: int, nbytes: int, settings: Any) -> int:
    """Worker processes for ``count`` selected records of ``nbytes`` bytes: ``want`` when given, else
    automatic -- the rule of the bulk previews and the JSON Patch export (``bulk.auto_workers``, when
    ``bulk._spawn_safe()``; below ``bulk.POOL_MIN_BYTES`` one)."""
    if want is not None:
        return max(1, min(int(want), POOL_MAX_PROCESSES))
    if count <= 0:
        return 1
    from . import bulk     # lazy: bulk imports much of the package

    if not bulk._spawn_safe():
        return 1
    return max(1, int(bulk.auto_workers(nbytes, settings)))


def _pool_workers(ctx: _Ctx) -> int:
    """Worker processes of the export ``ctx``: ``export(workers=)``, else automatic."""
    return _auto_workers(ctx.want_workers, ctx.count, ctx.meter.unit, ctx.doc.settings)


def _codec_fast() -> bool:
    """Whether :func:`codec.loads` of this process uses its fast path (orjson after the core's number check);
    the workers decode the same way (the values are the same either way; the tests run both)."""
    if codec._fast_checker is None:
        codec._init_fast()
    return bool(codec._fast_checker)


def _pool_init_codec(fast: bool) -> None:
    import gc

    gc.disable()      # the values of a block are acyclic and freed by reference counting
    codec._init_fast()
    if not fast:
        codec._fast_checker = False


def _too_deep_maybe(raw: bytes) -> bool:
    """More than :data:`POOL_SAFE_BRACKETS` opening brackets: the record may be nested so deeply that whether
    it decodes depends on the stack of the process (~16 800 levels for the json module), so a worker leaves it
    to the calling thread."""
    return len(raw) > POOL_SAFE_BRACKETS and raw.count(b"[") + raw.count(b"{") > POOL_SAFE_BRACKETS


class _Pool:
    """The worker processes of one pass (``ProcessPoolExecutor``, spawn: the rule of ``engine.apply``,
    ``bulk.preview`` and the JSON Patch export). :meth:`submit` and :meth:`result` return ``None`` when the
    caller must do the job itself: the workers cannot run (``BrokenProcessPool`` -- every later job too) or
    the job failed in the worker (``("redo",)``, or an exception the worker could not report); so a job's
    error is raised by the calling thread exactly as without workers. ``cancel`` is checked every
    :data:`POOL_WAIT` seconds while a result is awaited."""

    def __init__(self, workers: int, init: Callable[..., None], initargs: tuple, cancel: CancelToken | None,
                 what: str) -> None:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor

        self.workers = workers
        self.cancel = cancel
        self.what = what
        self.broken = False
        self.ex: Any = None
        try:
            self.ex = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"),
                                          initializer=init, initargs=initargs)
        except (OSError, ValueError, RuntimeError) as exc:   # pragma: no cover - no processes here
            self._broken(exc)

    def _broken(self, exc: BaseException) -> None:
        if not self.broken:
            log.warning("%s: the worker processes failed (%s); the rest is done in one thread", self.what, exc)
        self.broken = True

    def submit(self, fn: Callable[..., tuple], *args: Any) -> Any:
        from concurrent.futures.process import BrokenProcessPool

        if self.ex is None or self.broken:
            return None
        try:
            return self.ex.submit(fn, *args)
        except (BrokenProcessPool, RuntimeError) as exc:
            self._broken(exc)
            return None

    def result(self, fut: Any) -> tuple | None:
        from concurrent.futures.process import BrokenProcessPool

        if fut is None:
            return None
        try:
            while True:
                try:
                    res = fut.result(timeout=POOL_WAIT)
                    break
                except TimeoutError:
                    check(self.cancel)
        except BrokenProcessPool as exc:
            self._broken(exc)
            return None
        except Cancelled:
            raise
        except Exception:  # noqa: BLE001 - e.g. a result the worker could not send: the job is redone here
            log.debug("%s: a job failed in a worker process; it is redone here", self.what, exc_info=True)
            return None
        return None if res[0] == "redo" else res

    def close(self, futures: Iterable[Any] = ()) -> None:
        """Stop the workers (results not taken are dropped; a running job is finished first)."""
        for fut in futures:
            if fut is not None:
                fut.cancel()
        ex, self.ex = self.ex, None
        if ex is not None:
            ex.shutdown(wait=True, cancel_futures=True)


# -- the rows of CSV and XLSX exports -------------------------------------------------------
class _RowsJob:
    """The rows of a block of records: what a worker process does for :func:`_export_csv` /
    :func:`_export_xlsx` (and the calling thread with a block a worker could not do), with the per-record code
    of :func:`_iter_row_batches` (decode, :class:`_RowBuilder` with the export's cells). CSV: the rows as the
    text of the :mod:`csv` writer, encoded as :func:`_flush_text` does (``utf-8``, ``backslashreplace``); XLSX:
    the rows themselves (typed cells of :class:`_XlsxCells`; the calling thread writes them with xlsxwriter).

    :meth:`run` returns ``("ok", pieces, rows, found, stop, as_text)``: ``pieces`` -- CSV text (``bytes``) or
    lists of XLSX rows, and the numbers ``k`` of the block's records the calling thread does itself (in order:
    records that cannot be decoded in memory or whose rows fail to build, and records :func:`_too_deep_maybe`);
    ``rows`` -- rows in the pieces; ``found`` -- the positions of ``pending`` (explicit columns not found so
    far) that found a value; ``stop`` -- the first record not done (the end of the block, or where the pieces
    reached :data:`POOL_JOB_TEXT` characters of CSV / :data:`POOL_JOB_CELLS` XLSX cells); ``as_text`` -- XLSX
    numbers the pieces hold as their text (``writer.warn.xlsx_number_text``). A job builds at most
    :data:`POOL_JOB_ROWS` rows at a time and encodes its CSV text every :data:`POOL_PIECE_CHARS` characters,
    so a worker needs a few MB besides the interpreter (measured: 3-5 MB for a block of 2 MiB)."""

    def __init__(self, fmt: str, columns: list[_Column], explode: tuple | None, sep: str, delimiter: str,
                 track: bool, member: bool, cell_budget: int) -> None:
        self.csv = fmt == "csv"
        if self.csv:
            self.cells: Any = _TextCells(sep)
        elif fmt == "parquet":
            from .writer_parquet import _ParquetCells   # (the rows are typed cells, the XLSX way)

            self.cells = _ParquetCells(sep)
        else:
            self.cells = _XlsxCells(sep)
        self.builder = _RowBuilder(columns, self.cells, explode, sep, track=track)
        self.probes = dict(self.builder.unfound)
        self.member = member
        self.split: Callable[[bytes], tuple[str, int]] | None = None
        if member:
            from .reader import split_member

            self.split = split_member
        self.buf = io.StringIO()
        self.writer = _csv_writer(self.buf, delimiter)
        self.limit = max(1, min(ROW_BATCH, POOL_JOB_ROWS, cell_budget // max(1, self.builder.width)))

    def run(self, data: bytes, spans: bytes, indices: Any, start: int, pending: tuple) -> tuple:
        builder, cells = self.builder, self.cells
        probes = self.probes
        builder.unfound = {pos: probes[pos] for pos in pending if pos in probes}
        as_text = getattr(cells, "numbers_as_text", 0)
        if type(indices) is not array:
            indices = _pairs(indices)
        sp = _pairs(spans)
        n = len(indices)
        csv_rows = self.csv
        buf, writer, limit = self.buf, self.writer, self.limit
        width = max(1, builder.width)
        cap = POOL_JOB_TEXT if csv_rows else POOL_JOB_CELLS
        loads, member, split = codec.loads, self.member, self.split
        join_row, rows_of = builder.join_row, builder.rows
        fast = builder.explode is None and not member
        deep = _too_deep_maybe
        pieces: list[Any] = []
        out: list[list[Any]] = []
        nrows = 0
        size = 0                       # characters (CSV) / cells (XLSX) of the pieces so far

        def piece() -> None:
            nonlocal out, nrows, size
            if out:
                nrows += len(out)
                if csv_rows:
                    _write_rows(writer, out)
                else:
                    pieces.append(out)
                    size += len(out) * width
                out = []
            if csv_rows and buf.tell():
                chunk = buf.getvalue()
                size += len(chunk)
                pieces.append(chunk.encode("utf-8", "backslashreplace"))
                buf.seek(0)
                buf.truncate(0)

        k = start
        while k < n:
            raw = data[sp[2 * k]:sp[2 * k + 1]]
            if deep(raw):
                piece()
                pieces.append(k)
            else:
                try:
                    if fast:
                        out.append(join_row(loads(raw), None, indices[k]))
                    else:
                        key, value = _decode(raw, member, split)
                        out.extend(rows_of(value, key, indices[k]))
                except _DECODE_ERRORS:
                    piece()
                    pieces.append(k)
            k += 1
            if len(out) >= limit:
                if csv_rows:
                    _write_rows(writer, out)
                    nrows += len(out)
                    out = []
                    if buf.tell() >= POOL_PIECE_CHARS:
                        piece()            # (the buffer may hold 4 bytes per character)
                    if size + buf.tell() >= cap:
                        break
                else:
                    piece()
                    if size >= cap:
                        break
        piece()
        found = [pos for pos in pending if pos in probes and pos not in builder.unfound]
        return ("ok", pieces, nrows, found, k, getattr(cells, "numbers_as_text", 0) - as_text)


def _rows_pool_init(spec: tuple, fast: bool) -> None:
    """Worker process start: the job of the export (:class:`_RowsJob` from ``spec``) and the caller's decoding."""
    _pool_init_codec(fast)
    _POOL_STATE["rows"] = _RowsJob(*spec)


def _rows_pool_block(data: bytes, spans: bytes, indices: bytes, start: int, pending: tuple) -> tuple:
    """One job in a worker process: :meth:`_RowsJob.run`, or ``("redo",)`` when anything failed (the calling
    thread then does the block itself, so an error is raised exactly as without workers)."""
    try:
        return _POOL_STATE["rows"].run(data, spans, indices, start, pending)
    except Exception:  # noqa: BLE001 - redone (and raised) by the calling thread
        log.debug("a rows job failed in a worker process; the calling thread redoes it", exc_info=True)
        return ("redo",)


class _CsvOut:
    """Where :class:`_RowsPool` puts the rows of a CSV export: the jobs' text straight into the file, rows made
    in the calling thread through the export's :mod:`csv` writer (its buffer -- the header too -- is written
    out before the next job's text, so the order is kept)."""

    def __init__(self, csv_writer: Any, buf: io.StringIO, sink: _Sink) -> None:
        self.writer = csv_writer
        self.buf = buf
        self.sink = sink

    def piece(self, text: bytes) -> None:
        _flush_text(self.buf, self.sink)
        self.sink.write(text)

    def rows(self, rows: list[list[Any]]) -> None:
        _write_rows(self.writer, rows)
        if self.buf.tell() >= CSV_FLUSH_CHARS:
            _flush_text(self.buf, self.sink)

    def flush(self) -> None:
        _flush_text(self.buf, self.sink)


class _XlsxOut:
    """Where :class:`_RowsPool` puts the rows of an XLSX export: the workbook (xlsxwriter in the calling thread,
    the rows in their order -- the workbook is the same as without workers)."""

    def __init__(self, book: _XlsxBook) -> None:
        self.book = book

    def piece(self, rows: list[list[Any]]) -> None:
        self.book.write_rows(rows)

    def rows(self, rows: list[list[Any]]) -> None:
        self.book.write_rows(rows)

    def flush(self) -> None:
        pass


class _RowsPool:
    """The rows of a CSV / XLSX export built by worker processes (module docstring, "CSV and XLSX"): the calling
    thread reads the blocks (:data:`POOL_BLOCK_BYTES`), keeps at most ``2 * workers`` jobs in flight, puts the
    results in block order into ``out`` (:class:`_CsvOut` / :class:`_XlsxOut`) and does itself what
    :class:`_RowsJob` leaves to it -- huge records and the records a job names -- with the code of the
    one-thread export. A job a worker could not do, and every job once the workers broke
    (``BrokenProcessPool``), is done in the calling thread."""

    def __init__(self, ctx: _Ctx, builder: _RowBuilder, out: Any, workers: int) -> None:
        self.ctx = ctx
        self.builder = builder
        self.out = out
        self.workers = workers
        self.inflight = max(2, 2 * workers)
        opts = ctx.opts
        self.explicit = opts.columns is not None
        self.member = ctx.member
        self.split = ctx.doc.split_member
        self.spec = (opts.fmt, list(ctx.columns), ctx.explode, opts.join_sep, opts.csv_delimiter, self.explicit,
                     ctx.member, ctx.cell_budget)
        self.pool: _Pool | None = None
        self.local: _RowsJob | None = None
        self.waiting: deque = deque()

    def run(self) -> None:
        ctx = self.ctx
        pool = self.pool = _Pool(self.workers, _rows_pool_init, (self.spec, _codec_fast()), ctx.cancel,
                                 f"{FORMAT_NAMES[ctx.opts.fmt]} export")
        blocks = ctx.doc.iter_blocks(ctx.src, block_bytes=POOL_BLOCK_BYTES, cancel=ctx.cancel)
        try:
            for blk in blocks:
                check(ctx.cancel)
                if blk.huge:
                    self._drain()
                    self._huge(blk)
                    continue
                self._submit(blk, 0)
                while len(self.waiting) >= self.inflight:
                    self._take()
            self._drain()
        finally:
            blocks.close()
            waiting, self.waiting = self.waiting, deque()
            pool.close(fut for fut, _blk, _start in waiting)
        ctx.workers = max(ctx.workers, 1 if pool.broken else self.workers)

    def _pending(self) -> tuple:
        return tuple(self.builder.unfound) if self.builder.unfound else ()

    def _submit(self, blk: Block, start: int, front: bool = False) -> None:
        """A job for the records ``start..`` of ``blk`` (``front``: the rest of the job being written, so it
        goes before the later jobs); done here when there are no workers."""
        assert self.pool is not None
        fut = self.pool.submit(_rows_pool_block, blk.data, blk.spans, blk.indices.tobytes(), start,
                               self._pending())
        if fut is not None:
            if front:
                self.waiting.appendleft((fut, blk, start))
            else:
                self.waiting.append((fut, blk, start))
            return
        if not front:
            self._drain()                 # the jobs in flight come first (redone here: the workers broke)
        self._run_here(blk, start)

    def _run_here(self, blk: Block, start: int) -> None:
        if self.local is None:
            self.local = _RowsJob(*self.spec)
        job = self.local
        while True:
            check(self.ctx.cancel)
            res = job.run(blk.data, blk.spans, blk.indices, start, self._pending())
            start = self._write(blk, res)
            if start >= len(blk.indices):
                return

    def _drain(self) -> None:
        while self.waiting:
            self._take()

    def _take(self) -> None:
        assert self.pool is not None
        fut, blk, start = self.waiting.popleft()
        res = self.pool.result(fut)
        if res is None:
            self._run_here(blk, start)
            return
        stop = self._write(blk, res)
        if stop < len(blk.indices):
            self._submit(blk, stop, front=True)

    def _write(self, blk: Block, res: tuple) -> int:
        """Put a job's result out (in order); the first record not done is returned (a whole block: its records
        and progress are counted)."""
        _tag, pieces, rows, found, stop, as_text = res
        unfound = self.builder.unfound
        for pos in found:
            unfound.pop(pos, None)
        if as_text:
            self.builder.cells.numbers_as_text += as_text
        ctx, out = self.ctx, self.out
        for p in pieces:
            if type(p) is int:
                self._record(blk, p)
            else:
                out.piece(p)
        ctx.rows += rows
        if stop >= len(blk.indices):
            ctx.records += len(blk.indices)
            ctx.advance(blk)
        return stop

    def _record(self, blk: Block, k: int) -> None:
        """Record ``k`` of ``blk`` exactly as :func:`_iter_row_batches` does it (decoded here; when it cannot
        be: streamed with explicit columns, skipped with automatic ones)."""
        ctx, builder = self.ctx, self.builder
        s, e = blk.span(k)
        index = int(blk.indices[k])
        try:
            key, value = _decode(blk.data[s:e], self.member, self.split)
            rows = builder.rows(value, key, blk.indices[k])
        except _DECODE_ERRORS:
            if self.explicit:
                for batch in builder.streamed_rows(ctx, index, ctx.doc.record_span(index)):
                    self._rows(batch)
            else:
                ctx.skip_deep("writer.warn.deep_skipped")
            ctx.records -= 1               # (counted with the block)
        else:
            self._rows(rows)
        self.out.flush()

    def _rows(self, rows: list[list[Any]]) -> None:
        self.out.rows(rows)
        self.ctx.rows += len(rows)

    def _huge(self, blk: Block) -> None:
        """A huge record, as :func:`_iter_row_batches` does it."""
        ctx = self.ctx
        if self.explicit:
            for batch in self.builder.streamed_rows(ctx, int(blk.indices[0]), blk.span(0)):
                self._rows(batch)
        else:
            ctx.skip_huge("writer.warn.huge_skipped")
        self.out.flush()
        ctx.advance(blk)


# -- the column scan -------------------------------------------------------------------------
def _scan_summary(scan: _ColumnScan) -> tuple:
    """What :class:`_ScanMerge` needs of a scan of a run of records: the kinds of the paths (first-seen
    order) of the records and of the expanded array's elements, the collapsed and deep paths, the counts,
    the keys and first widths of the object paths, and whether the scan left the regime the merge is exact
    for (a map, a shrink)."""
    nodes = []
    stack = [scan.main.root] + ([scan.elements.root] if scan.elements is not None else [])
    while stack:
        node = stack.pop()
        if node.keys:
            nodes.append((node.path, tuple(node.keys), node.first))
        stack.extend(node.kids.values())
    return (list(scan.main.kinds.items()),
            None if scan.elements is None else list(scan.elements.kinds.items()),
            list(scan.collapsed), list(scan.deep), scan.records, scan.undecodable, nodes,
            bool(scan.map_nodes) or scan.depth != MAX_DEPTH)


class _ScanMerge:
    """The column scan of a selection assembled from scans of consecutive runs of its records, in order
    (:class:`_ScanJob` in worker processes).

    It equals the one-thread :class:`_ColumnScan` as long as no object path becomes a map and the scan never
    shrinks (not in inferred rows mode, which :func:`_discover` keeps in one thread): the paths and their
    kinds are then the union of the runs' (in first-seen order: a run's new paths follow the earlier runs'),
    an object is collapsed at the same depth in every run (bits a run marks at a collapsed path are replaced
    by the collapsed column), and the counts add up. The regime is checked conservatively: a run that saw a
    map or shrank; more than ``SCAN_MAX_PATHS`` paths; or an object path whose different keys (all runs so
    far) would make it map-like even at the width of its first object (the one-thread scan tests every object
    with its running key count, at most the final one, and its running width, at least the first object's:
    :func:`_map_like` grows with the first and falls with the second). :attr:`ok` is ``False`` then and the
    caller scans in one thread instead."""

    def __init__(self, rows: bool, explode: tuple | None) -> None:
        self.scan = _ColumnScan(rows, explode)
        self.keys: dict[tuple, set[str]] = {}
        self.first: dict[tuple, int] = {}
        self.ok = True

    def add(self, summary: tuple) -> bool:
        main, elements, collapsed, deep, records, undecodable, nodes, irregular = summary
        if irregular:
            self.ok = False
            return False
        scan = self.scan
        kinds = scan.main.kinds
        for p, bits in main:
            old = kinds.get(p)
            kinds[p] = bits if old is None else old | bits
        tracked = len(kinds)
        if scan.elements is not None:
            ekinds = scan.elements.kinds
            for p, bits in elements or ():
                old = ekinds.get(p)
                ekinds[p] = bits if old is None else old | bits
            tracked += len(ekinds)
        for p in collapsed:
            scan.collapsed.setdefault(p, None)
        for p in deep:
            scan.deep.setdefault(p, None)
        scan.records += records
        scan.undecodable += undecodable
        scan.tracked = tracked
        if tracked > SCAN_MAX_PATHS:
            self.ok = False
        explode = scan.explode
        for path, keys, first in nodes:
            have = self.keys.get(path)
            if have is None:
                have = self.keys[path] = set()
            if first and not self.first.get(path):
                self.first[path] = first
            have.update(keys)
            if len(have) > MAP_MIN_KEYS and path != explode and _map_like(len(have), self.first.get(path) or 1):
                self.ok = False
        return self.ok


class _ScanJob:
    """The column scan of a block of records in a worker process (and of the records a worker leaves to the
    calling thread): :class:`_ColumnScan` s of the runs of records between those records, decoded as
    :func:`_discover` decodes them, as :func:`_scan_summary` summaries."""

    def __init__(self, rows: bool, explode: tuple | None, member: bool) -> None:
        self.rows = rows
        self.explode = explode
        self.member = member
        self.split: Callable[[bytes], tuple[str, int]] | None = None
        if member:
            from .reader import split_member

            self.split = split_member

    def run(self, data: bytes, spans: bytes) -> tuple:
        """``("ok", parts)``: summaries and the numbers of the records the calling thread scans itself
        (:func:`_too_deep_maybe`), in order."""
        sp = _pairs(spans)
        member, split, deep = self.member, self.split, _too_deep_maybe
        oloads, oerror = orjson.loads, orjson.JSONDecodeError
        parts: list[Any] = []
        scan: _ColumnScan | None = None
        for k in range(len(sp) // 2):
            raw = data[sp[2 * k]:sp[2 * k + 1]]
            if member:
                assert split is not None
                raw = raw[split(raw)[1]:]
            if deep(raw):
                if scan is not None:
                    parts.append(_scan_summary(scan))
                    scan = None
                parts.append(k)
                continue
            if scan is None:
                scan = _ColumnScan(self.rows, self.explode)
            try:
                value = oloads(raw)          # (as _discover)
            except oerror:
                try:
                    value = codec.loads(raw)
                except _DECODE_ERRORS:
                    scan.undecodable += 1
                    continue
            scan.add(value)
        if scan is not None:
            parts.append(_scan_summary(scan))
        return ("ok", parts)

    def one(self, raw: bytes) -> tuple:
        """The summary of one record, scanned here (whatever its nesting)."""
        scan = _ColumnScan(self.rows, self.explode)
        if self.member:
            assert self.split is not None
            raw = raw[self.split(raw)[1]:]
        try:
            value = orjson.loads(raw)
        except orjson.JSONDecodeError:
            try:
                value = codec.loads(raw)
            except _DECODE_ERRORS:
                scan.undecodable += 1
                return _scan_summary(scan)
        scan.add(value)
        return _scan_summary(scan)


def _scan_pool_init(rows: bool, explode: tuple | None, member: bool, fast: bool) -> None:
    """Worker process start: the scan job and the caller's decoding."""
    _pool_init_codec(fast)
    _POOL_STATE["scan"] = _ScanJob(rows, explode, member)


def _scan_pool_block(data: bytes, spans: bytes) -> tuple:
    """One scan job in a worker process (``("redo",)`` when anything failed: the calling thread redoes it)."""
    try:
        return _POOL_STATE["scan"].run(data, spans)
    except Exception:  # noqa: BLE001 - redone (and raised) by the calling thread
        log.debug("a column-scan job failed in a worker process; the calling thread redoes it", exc_info=True)
        return ("redo",)


class _ScanPool:
    """The column scan of a whole selection in worker processes: blocks of :data:`POOL_BLOCK_BYTES`, at most
    ``2 * workers`` in flight, merged in order (:class:`_ScanMerge`); ``on_block`` (progress) is called for
    every block in order. :meth:`run` returns ``None`` when the merge cannot be exact (the caller scans in
    one thread from the start; :meth:`catch_up` keeps its progress from going back)."""

    def __init__(self, doc: Document, src: Any, rows: bool, explode: tuple | None, cancel: CancelToken | None,
                 on_block: Callable[[Any], None] | None, workers: int) -> None:
        self.doc = doc
        self.src = src
        self.rows = rows
        self.explode = explode
        self.cancel = cancel
        self.on_block = on_block
        self.workers = workers
        self.reported = 0                  # records of the blocks reported to on_block

    def run(self) -> _ColumnScan | None:
        doc, cancel = self.doc, self.cancel
        member = bool(doc.member_records)
        merge = _ScanMerge(self.rows, self.explode)
        job = _ScanJob(self.rows, self.explode, member)
        pool = _Pool(self.workers, _scan_pool_init, (self.rows, self.explode, member, _codec_fast()), cancel,
                     "column scan")
        inflight = max(2, 2 * self.workers)
        waiting: deque = deque()
        blocks = doc.iter_blocks(self.src, block_bytes=POOL_BLOCK_BYTES, cancel=cancel)
        try:
            for blk in blocks:
                check(cancel)
                fut = None if blk.huge else pool.submit(_scan_pool_block, blk.data, blk.spans)
                waiting.append((fut, blk))
                while len(waiting) >= inflight:
                    if not self._take(waiting.popleft(), pool, job, merge):
                        return None
            while waiting:
                if not self._take(waiting.popleft(), pool, job, merge):
                    return None
        finally:
            blocks.close()
            pool.close(fut for fut, _blk in waiting)
        merge.scan.workers = 1 if pool.broken else self.workers
        return merge.scan

    def _take(self, entry: tuple, pool: _Pool, job: _ScanJob, merge: _ScanMerge) -> bool:
        fut, blk = entry
        if not blk.huge:                   # (huge records are not scanned: _discover)
            res = pool.result(fut)
            if res is None:
                check(self.cancel)
                res = job.run(blk.data, blk.spans)
            for part in res[1]:
                if type(part) is int:
                    s, e = blk.span(part)
                    part = job.one(blk.data[s:e])
                if not merge.add(part):
                    log.info("column scan: an object path may be map-like or the paths are many; the scan is "
                             "done again in one thread")
                    return False
        self.reported += len(blk.indices)
        if self.on_block is not None:
            self.on_block(blk)
        return True

    def catch_up(self, on_block: Callable[[Any], None] | None) -> Callable[[Any], None] | None:
        """``on_block`` for the one-thread scan done after :meth:`run` gave up: the blocks of the records
        already reported are not reported again (the progress never goes back)."""
        if on_block is None or not self.reported:
            return on_block
        skip = self.reported
        seen = 0

        def report(blk: Any) -> None:
            nonlocal seen
            seen += len(blk.indices)
            if seen > skip:
                on_block(blk)

        return report


# ----------------------------------------------------------------------------------
# XLSX
# ----------------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a dependency
        return True
    try:
        return bool(psutil.pid_exists(pid))
    except Exception:  # pragma: no cover - be conservative
        return True


def _staging_roots(settings: Settings) -> list[Path]:
    roots = []
    if settings.cache_dir:
        roots.append(Path(settings.cache_dir) / "tmp")
    try:
        roots.append(tmp_dir())
    except OSError:  # pragma: no cover - app dir not creatable
        pass
    return roots


def _purge_staging(settings: Settings) -> int:
    """Remove XLSX staging folders (``xlsx-<pid>-<hex>``) of processes that no longer run,
    and old-style ``xlsx-xxxxxxxx`` folders older than a day. Never raises; returns the
    number of folders removed."""
    removed = 0
    me = os.getpid()
    now = time.time()
    for root in _staging_roots(settings):
        try:
            names = os.listdir(long_path(root))
        except OSError:
            continue
        for name in names:
            m = _STAGING_RE.match(name)
            p = root / name
            if m is not None:
                pid = int(m.group(1))
                if pid == me or _pid_alive(pid):
                    continue
            elif _LEGACY_STAGING_RE.match(name):
                try:
                    if now - os.stat(long_path(p)).st_mtime < STALE_LEGACY_AGE:
                        continue
                except OSError:
                    continue
            else:
                continue
            try:
                shutil.rmtree(long_path(p))
                removed += 1
                log.info("removed stale XLSX staging folder %s", p)
            except OSError:
                log.debug("cannot remove stale %s", p, exc_info=True)
    return removed


def _staging_dir(settings: Settings) -> str:
    """A new ``xlsx-<pid>-<hex>`` folder under ``work_dir(cache_dir)`` (long-path form)."""
    try:
        root = work_dir(settings.cache_dir)
    except OSError:
        if not settings.cache_dir:
            raise
        root = Path(settings.cache_dir) / "tmp"
        try:
            os.makedirs(long_path(root), exist_ok=True)
        except OSError as exc:
            raise os_error(exc, root) from exc
    for _ in range(100):
        p = root / f"xlsx-{os.getpid()}-{secrets.token_hex(4)}"
        try:
            os.mkdir(long_path(p))
            return long_path(p)
        except FileExistsError:
            continue
        except OSError as exc:
            raise os_error(exc, p) from exc
    raise os_error(FileExistsError(str(root)), root)  # pragma: no cover


def _remove_quiet(p: StrPath) -> None:
    try:
        os.remove(long_path(p))
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("could not remove temporary file %s", p, exc_info=True)


def _replace_retry(src: Path, dst: Path, timeout: float = 1.0) -> None:
    """:func:`jsonreshape.fileio.move_replace`: a POSIX-semantics rename (a target this program
    still reads is replaced too), else ``os.replace``; retried briefly on ``PermissionError``
    (Windows: antivirus scanners and indexers hold new files for a moment). Raises the last
    ``OSError``."""
    move_replace(src, dst, timeout=timeout)


def _part_error(exc: OSError, path: Path) -> Exception:
    if isinstance(exc, PermissionError) and not os.path.isdir(long_path(path)):
        return ExportError("writer.error.xlsx_part_locked", path=str(path))
    return os_error(exc, path)


def _commit_parts(staged: list[Path], names: list[Path]) -> None:
    """Move the staged parts to ``names``, all or nothing (:func:`jsonreshape.fileio.commit_files`:
    existing targets are renamed aside first; when any step fails the new parts are removed and
    the old files restored; a part another program holds -> ``writer.error.xlsx_part_locked``)."""
    commit_files(staged, names, move=lambda s, d: _replace_retry(s, d), error=_part_error)


class _XlsxSink:
    """The file object xlsxwriter writes a workbook (zip) to: ``cancel`` is checked for
    every piece, the bytes written are reported, and after an error (or once the file is
    closed) every further call only moves a virtual position -- a ZipFile kept alive by
    the error's traceback can then finish its ``close()``/``__del__`` without touching the
    file and without failing ("Exception ignored in ZipFile.__del__")."""

    def __init__(self, f: Any, cancel: CancelToken | None) -> None:
        self._f = f
        self._cancel = cancel
        self.on_write: Callable[[int], None] | None = None
        self.dead = False
        self.written = 0
        self._pos = 0           # the position of the (fresh, empty) file, mirrored
        self._end = 0

    def write(self, data: Any) -> int:
        n = len(data)
        if not self.dead:
            try:
                check(self._cancel)
                self._f.write(data)
            except BaseException:
                self.dead = True
                raise
            self.written += n
            if self.on_write is not None:
                self.on_write(self.written)
        self._pos += n
        if self._pos > self._end:
            self._end = self._pos
        return n

    def tell(self) -> int:
        return self._pos

    def seek(self, pos: int, whence: int = 0) -> int:
        if not self.dead:
            try:
                self._f.seek(pos, whence)
            except BaseException:
                self.dead = True
                raise
        if whence == 1:
            pos += self._pos
        elif whence == 2:
            pos += self._end
        self._pos = pos
        return pos

    def flush(self) -> None:
        if not self.dead:
            try:
                self._f.flush()
            except BaseException:
                self.dead = True
                raise

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True


class _XlsxBook:
    """Rows -> one or more ``constant_memory`` workbooks: new sheets (``sheets``) or new
    files (``files``, staged in the target directory) when ``xlsx_max_rows`` is reached."""

    def __init__(self, ctx: _Ctx, header: list[str], first_path: Path | None) -> None:
        import xlsxwriter  # deferred: only needed for XLSX exports

        self._xlsxwriter = xlsxwriter
        self.ctx = ctx
        self.header = header
        self.max_rows = ctx.opts.xlsx_max_rows
        self.files_mode = first_path is None
        self.first_path = first_path
        self.check_surrogates = ctx.check_surrogates
        self.tmpdir = _staging_dir(ctx.doc.settings)
        self.staged: list[Path] = []
        self.wb: Any = None
        self.ws: Any = None
        self.bold: Any = None
        self.fh: Any = None
        self.sink: _XlsxSink | None = None
        self.row = 0
        self.sheets = 0
        self.truncated = 0

    # -- workbooks and sheets -----------------------------------------------------------
    def _stage(self) -> Path:
        out = self.ctx.out
        fd, name = tempfile.mkstemp(prefix=f".{out.name[:48]}.", suffix=".part", dir=long_path(out.parent))
        os.close(fd)
        path = Path(name)
        self.staged.append(path)
        return path

    def open(self) -> None:
        path = self._stage() if self.files_mode else self.first_path
        assert path is not None
        try:
            self.fh = open(long_path(path), "wb", buffering=WRITE_BUFFER)  # noqa: SIM115
        except OSError as exc:
            raise os_error(exc, self.ctx.out) from exc
        self.sink = _XlsxSink(self.fh, self.ctx.cancel)
        self.wb = self._xlsxwriter.Workbook(self.sink, {
            "constant_memory": True,
            "strings_to_numbers": False,
            "strings_to_formulas": False,
            "strings_to_urls": False,
            "tmpdir": self.tmpdir,
        })
        self.bold = self.wb.add_format({"bold": True})
        self._new_sheet()

    def _new_sheet(self) -> None:
        ws = self.wb.add_worksheet()
        ws.freeze_panes(1, 0)
        for c, text in enumerate(self.header):
            ws.write_string(0, c, self._clean(text), self.bold)
        self.ws = ws
        self.row = 1
        self.sheets += 1

    def close_workbook(self, final: bool = False) -> None:
        """Assemble the workbook (the zip) into its file; ``final``: the last one of the
        export (progress of this slow step: bytes written)."""
        wb, self.wb = self.wb, None
        if wb is None:
            return
        ctx = self.ctx
        exc_mod = self._xlsxwriter.exceptions
        try:
            check(ctx.cancel)
            if final:
                assert self.sink is not None
                self.sink.on_write = ctx.finishing()
            else:
                ctx.message("writer.progress.finish")
            wb.close()
        except exc_mod.FileSizeError:
            self._close_handles(wb)
            self._close_file()
            raise ExportError("writer.error.xlsx_too_large", path=str(ctx.out)) from None
        except exc_mod.FileCreateError as exc:
            self._close_handles(wb)
            self._close_file()
            cause = exc.args[0] if exc.args else None
            if isinstance(cause, OSError):
                raise os_error(cause, ctx.out) from exc
            raise ExportError("error.export", detail=str(exc)) from exc
        except BaseException:
            self._close_handles(wb)
            self._close_file()
            raise
        self._finish_file()
        if not final:
            ctx.message("writer.progress.export")

    def _finish_file(self) -> None:
        """Flush and close the file of a completed workbook (errors are reported)."""
        sink, fh = self.sink, self.fh
        self.sink = self.fh = None
        if sink is not None:
            sink.dead = True
        if fh is None:  # pragma: no cover
            return
        try:
            fh.flush()
            os.fsync(fh.fileno())
            fh.close()
        except OSError as exc:
            try:
                fh.close()
            except OSError:
                pass
            raise os_error(exc, self.ctx.out) from exc

    def _close_file(self) -> None:
        """Close the file of an unfinished workbook (quietly)."""
        sink, fh = self.sink, self.fh
        self.sink = self.fh = None
        if sink is not None:
            sink.dead = True
        if fh is not None:
            try:
                fh.close()
            except OSError:
                log.debug("closing an aborted XLSX file failed", exc_info=True)

    @staticmethod
    def _close_handles(wb: Any) -> None:
        """Close the row-data temp files of an unfinished ``constant_memory`` workbook (so
        its temp directory can be removed on Windows)."""
        for ws in wb.worksheets():
            fh = getattr(ws, "row_data_fh", None)
            if fh is not None:
                try:
                    fh.close()
                except Exception:  # pragma: no cover - best effort
                    pass

    # -- rows ----------------------------------------------------------------------------
    def _clean(self, s: str) -> str:
        if self.check_surrogates and not s.isascii() and _SURROGATE_RE.search(s):
            s = _escape_surrogates(s)
        if len(s) > _XLSX_SAFE_CHARS:
            data = s.encode("utf-16-le", "surrogatepass")
            if len(data) > 2 * XLSX_MAX_STRING:
                data = data[:2 * XLSX_MAX_STRING]
                if 0xD8 <= data[-1] <= 0xDB:  # do not split a surrogate pair
                    data = data[:-2]
                s = data.decode("utf-16-le", "surrogatepass")
                self.truncated += 1
        return s

    def write_rows(self, rows: list[list[Any]]) -> None:
        clean_all = self.check_surrogates
        for row in rows:
            if self.row >= self.max_rows:
                if self.files_mode:
                    self.close_workbook()
                    self.open()
                else:
                    self._new_sheet()
            ws = self.ws
            r = self.row
            for c, v in enumerate(row):
                if v is None:
                    continue
                tv = type(v)
                if tv is str:
                    if not v:
                        continue
                    if clean_all or len(v) > _XLSX_SAFE_CHARS:
                        v = self._clean(v)
                    ws.write_string(r, c, v)
                elif tv is bool:
                    ws.write_boolean(r, c, v)
                else:
                    ws.write_number(r, c, v)
            self.row = r + 1

    # -- finishing -------------------------------------------------------------------------
    def commit_files(self) -> list[Path]:
        """``files`` mode: move the staged parts to their final names (all or nothing)."""
        ctx = self.ctx
        out = ctx.out
        n = len(self.staged)
        if n == 1:
            names = [out]
        else:
            names = [out.with_name(f"{out.stem}_{k}{out.suffix}") for k in range(1, n + 1)]
        for final in names:
            _check_target(ctx.doc, final)
        stale = []
        k = n + 1 if n > 1 else 1
        while True:
            p = out.with_name(f"{out.stem}_{k}{out.suffix}")
            if not os.path.lexists(long_path(p)):
                break
            stale.append(p)
            k += 1
        ctx.commit_check(names)          # (the part names: a part with unsaved edits is never replaced)
        _commit_parts(self.staged, names)
        self.staged = []
        if stale:
            ctx.warn("writer.warn.xlsx_stale_parts", add=len(stale),
                     names=", ".join(p.name for p in stale[:5]) + (", …" if len(stale) > 5 else ""))
        return names

    def abort(self) -> None:
        if self.wb is not None:
            self._close_handles(self.wb)
            self.wb = None
        self._close_file()
        for p in self.staged:
            _remove_quiet(p)
        self.staged = []

    def cleanup(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def _xlsx_rows(ctx: _Ctx, builder: _RowBuilder, book: _XlsxBook) -> None:
    """The rows of an XLSX export into ``book``: when the export may use worker processes, at most
    :data:`XLSX_POOL_WORKERS` of them prepare the cells (:class:`_RowsPool`; xlsxwriter in this thread is the
    bound, more would only cost memory), else this thread does. The rows reach xlsxwriter in the same order
    either way."""
    workers = _pool_workers(ctx)
    if workers > 1:
        _RowsPool(ctx, builder, _XlsxOut(book), min(workers, XLSX_POOL_WORKERS)).run()
        return
    for batch in _iter_row_batches(ctx, builder):
        book.write_rows(batch)
        ctx.rows += len(batch)


def _export_xlsx(ctx: _Ctx, out: Path) -> list[Path]:
    opts = ctx.opts
    if len(ctx.columns) > XLSX_MAX_COLUMNS:
        raise UserInputError("writer.error.too_many_columns", max=XLSX_MAX_COLUMNS, count=len(ctx.columns))
    cells = _XlsxCells(opts.join_sep)
    builder = _builder(ctx, cells)
    header = [c.text for c in ctx.columns]
    files: list[Path]
    if opts.xlsx_split == "sheets":
        with atomic_path(out) as tmp:
            book = _XlsxBook(ctx, header, tmp)
            try:
                book.open()
                _xlsx_rows(ctx, builder, book)
                book.close_workbook(final=True)
                ctx.commit_check()
            except BaseException:
                book.abort()
                raise
            finally:
                book.cleanup()
        files = [out]
        if book.sheets > 1:
            ctx.warn("writer.warn.xlsx_sheets", add=book.sheets)
    else:
        book = _XlsxBook(ctx, header, None)
        try:
            book.open()
            _xlsx_rows(ctx, builder, book)
            book.close_workbook(final=True)
            parts = len(book.staged)
            files = book.commit_files()
        except BaseException:
            book.abort()
            raise
        finally:
            book.cleanup()
        if parts > 1:
            ctx.warn("writer.warn.xlsx_files", add=parts)
    if cells.numbers_as_text:
        ctx.warn("writer.warn.xlsx_number_text", add=cells.numbers_as_text)
    if book.truncated:
        ctx.warn("writer.warn.xlsx_truncated", add=book.truncated, max=XLSX_MAX_STRING)
    _warn_unfound(ctx, builder)
    return files


# ----------------------------------------------------------------------------------
# SQLite
# ----------------------------------------------------------------------------------
def _q(name: str) -> str:
    """Quoted SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def _sql_name(text: str) -> str:
    """A safe identifier text: control characters and lone surrogates as ``\\uXXXX``."""
    if _NAME_BAD_RE.search(text):
        text = _NAME_BAD_RE.sub(lambda m: "\\u%04x" % ord(m.group()), text)
    return text or "_"


def _unique_name(registry: set[str], name: str) -> str:
    """``name`` or ``name_2``, ``name_3``, ... not yet in ``registry`` (SQLite compares
    identifiers ASCII-case-insensitively); registers the result."""
    if name.translate(_ASCII_LOWER).startswith("sqlite_"):
        name = "_" + name   # reserved for SQLite's internal objects
    candidate = name
    k = 2
    while candidate.translate(_ASCII_LOWER) in registry:
        candidate = f"{name}_{k}"
        k += 1
    registry.add(candidate.translate(_ASCII_LOWER))
    return candidate


def _table_name(schema: tuple) -> str:
    """Child table name: the dotted array path without the element markers that are
    followed by a key (``orders[*].items`` -> ``orders.items``; ``matrix[*]`` stays). The
    records themselves being arrays (schema ``()``) gives ``value``; arrays below them
    ``value[*]``, ``value.k``, ..."""
    segs = [s for i, s in enumerate(schema)
            if not (s is Wild.ANY_INDEX and i + 1 < len(schema) and isinstance(schema[i + 1], str))]
    text = format_path(tuple(segs)) if segs else ""
    if not schema or schema[0] is Wild.ANY_INDEX:
        if not text:
            return VALUE_NAME
        return VALUE_NAME + (text if text.startswith("[") else "." + text)
    return text


_FLOAT_INF = float("inf")


def _exact_double(v: JsonFloat) -> float | None:
    """The double of a ``JsonFloat`` when it has exactly the token's decimal value
    (``1.10``, ``1E3``, ``-0.00``, ``3.141592653589793``); ``None`` otherwise
    (``1e400``, ``1e-400``, ``123456789.123456789``)."""
    f = float(v)
    if f != f or f == _FLOAT_INF or f == -_FLOAT_INF:
        return None
    try:
        if Decimal(v.raw) == Decimal(repr(f)):
            return f
    except InvalidOperation:  # pragma: no cover - codec number texts are valid
        pass
    return None


class _SqlTable:
    __slots__ = ("name", "schema", "child", "depth", "prefix", "cols", "names", "width", "pending", "next_id", "sql",
                 "extra_pos", "extra_name", "extra_rows", "parent", "rows")

    def __init__(self, name: str, schema: tuple | None, fixed: list[str], parent: str | None = None) -> None:
        self.name = name
        self.schema = schema                    # array path (None: the records table)
        self.child = schema is not None
        self.depth = len(schema) + 1 if schema is not None else 0   # path depth of the row values
        self.prefix = schema + (Wild.ANY_INDEX,) if schema is not None else ()   # ... and their path
        self.cols: dict[tuple, int] = {}        # leaf path inside a row -> column position
        self.names = {n.translate(_ASCII_LOWER) for n in fixed}
        self.width = len(fixed)
        self.pending: list[list[Any]] = []
        self.next_id = 1
        self.sql: str | None = None
        self.extra_pos: int | None = None       # position of the overflow column once added ...
        self.extra_name = EXTRA_NAME            # ... and its name (_extra, or _extra_2 when taken)
        self.extra_rows = 0
        self.parent = parent                    # the table holding the parent rows (child tables)
        self.rows = 0                           # rows inserted


class _ObjStats:
    """The objects at one path of a SQLite export (map detection): their shapes (keys and
    value types) with counts, or -- beyond the shape budget -- per-key counts."""

    __slots__ = ("sigs", "keys", "distinct", "widest", "map")

    def __init__(self) -> None:
        self.sigs: dict[tuple, list] = {}       # (keys, value types) -> [objects, keys holding containers]
        self.keys: Counter[str] = Counter()     # keys of the objects whose shape was not remembered
        self.distinct: set[str] = set()         # every key seen
        self.widest = 0
        self.map: _MapStats | None = None

    def counts(self) -> Counter[str]:
        counts = self.keys
        for (keys, _types), e in self.sigs.items():
            n = e[0]
            for k in keys:
                counts[k] += n
        return counts


class _SqliteScan:
    """The first pass of a SQLite export: the objects at every path of every record
    (array elements: ``[*]``) are counted like those of the CSV/XLSX column scan, so
    maps are found before any row is written (module docstring, "SQLite"). Objects whose
    shape was seen before cost a lookup and a count."""

    def __init__(self) -> None:
        self.nodes: dict[tuple, _ObjStats] = {}
        self.maps: list[tuple] = []
        self.records = 0
        self.budget = SCAN_SIG_BUDGET

    def add(self, value: Any) -> None:
        self.records += 1
        records = self.records
        nodes = self.nodes
        stack: list[tuple[tuple, Any]] = [((), value)]
        while stack:
            path, v = stack.pop()
            tv = type(v)
            if tv is dict:
                deeper = len(path) + 1 < MAX_DEPTH
                node = nodes.get(path)
                if node is None:
                    if len(nodes) >= SCAN_MAX_PATHS:      # no statistics: just walk on
                        if deeper:
                            stack.extend((path + (k,), x) for k, x in v.items()
                                         if type(x) is dict or type(x) is list)
                        continue
                    node = nodes[path] = _ObjStats()
                m = node.map
                if m is not None:
                    m.add(v, records)
                    if deeper:
                        walk = m.walk
                        stack.extend((path + (k,), x) for k, x in v.items()
                                     if k in walk and (type(x) is dict or type(x) is list))
                    continue
                sig = (tuple(v), tuple(map(type, v.values())))
                e = node.sigs.get(sig)
                if e is None:
                    keys = sig[0]
                    node.distinct.update(keys)
                    if len(keys) > node.widest:
                        node.widest = len(keys)
                    cont = tuple(k for k, tk in zip(keys, sig[1]) if tk is dict or tk is list)
                    if self.budget > 0 and len(node.sigs) < SCAN_SIG_PER_NODE:
                        e = node.sigs[sig] = [1, cont]
                        self.budget -= 1
                    else:
                        node.keys.update(keys)
                    if _map_like(len(node.distinct), node.widest):
                        node.map = _MapStats(node.counts(), records, None, node.widest)
                        node.sigs, node.keys, node.distinct = {}, Counter(), set()
                        self.maps.append(path)
                else:
                    e[0] += 1
                    cont = e[1]
                if deeper:
                    for k in cont:
                        stack.append((path + (k,), v[k]))
            elif tv is list and len(path) + 1 < MAX_DEPTH:
                p = path + (Wild.ANY_INDEX,)
                for x in v:
                    tx = type(x)
                    if tx is dict or tx is list:
                        stack.append((p, x))

    def plans(self) -> dict[tuple, _MapPlan]:
        """The maps (path -> :class:`_MapPlan`) that pool some keys."""
        out = {}
        for path in self.maps:
            stats = self.nodes[path].map
            assert stats is not None
            plan = _map_plan(path, stats, self.records)
            if plan.pool is not None:
                out[path] = plan
        return out


class _Pooled:
    """The pooled members of a map object, stored as one JSON text after the kept ones."""

    __slots__ = ("value",)

    def __init__(self, value: dict) -> None:
        self.value = value


class _SqliteWriter:
    """Records -> the ``records`` table and one child table per array path (+ the ``_jsonreshape``
    table describing them and, for wrapped documents, the ``_wrapper`` table)."""

    def __init__(self, conn: sqlite3.Connection, ctx: _Ctx, plans: dict[tuple, _MapPlan] | None = None,
                 ordered: bool = False) -> None:
        self.conn = conn
        self.ctx = ctx
        self.member = ctx.member
        self.ordered = ordered                  # an "_ord" column: the selection's order
        self.check_surrogates = ctx.check_surrogates
        limit = SQLITE_MAX_COLUMNS
        try:
            limit = min(limit, conn.getlimit(sqlite3.SQLITE_LIMIT_COLUMN) - 2)
        except (AttributeError, sqlite3.Error):  # pragma: no cover - Python < 3.11
            pass
        self.max_columns = max(8, limit)
        self.cell_budget = ctx.cell_budget
        self.plans = plans or {}
        # tables and indexes; the names of the description tables are taken first, so a data table of
        # that name becomes "<name>_2"
        self.names: set[str] = {META_TABLE.translate(_ASCII_LOWER), WRAPPER_TABLE.translate(_ASCII_LOWER)}
        self.order: list[_SqlTable] = []
        self.children: dict[tuple, _SqlTable] = {}
        self.rows = 0
        self.numbers_as_text = 0
        self.deep_values = 0
        self.json_arrays = 0                    # arrays stored as JSON text (SQLITE_MAX_TABLES)
        self.json_array_paths: dict[str, None] = {}
        self.records = self._new_table("records", None)

    def _new_table(self, base: str, schema: tuple | None, parent: _SqlTable | None = None) -> _SqlTable:
        name = _unique_name(self.names, _sql_name(base))
        if schema is not None:
            assert parent is not None
            fixed = ["_id", "_parent", "_idx"]
            defs = (f'"_id" INTEGER PRIMARY KEY, "_parent" INTEGER REFERENCES {_q(parent.name)}("_id"), '
                    '"_idx" INTEGER')
        else:
            fixed = ["_id"] + (["_ord"] if self.ordered else []) + ([KEY_COLUMN] if self.member else [])
            defs = ", ".join(['"_id" INTEGER PRIMARY KEY'] + [f"{_q(c)}" + (" INTEGER" if c == "_ord" else "")
                                                             for c in fixed[1:]])
        self.conn.execute(f"CREATE TABLE {_q(name)} ({defs})")
        table = _SqlTable(name, schema, fixed, parent.name if parent is not None else None)
        self.order.append(table)
        return table

    def _table_for(self, parent: _SqlTable, rel: tuple) -> _SqlTable | None:
        """The child table of the array at ``rel`` in rows of ``parent`` (created when
        needed); ``None`` once :data:`SQLITE_MAX_TABLES` child tables exist (the array is
        then stored as JSON text in the parent row)."""
        schema = parent.prefix + rel
        table = self.children.get(schema)
        if table is None:
            if len(self.children) >= SQLITE_MAX_TABLES:
                self.json_arrays += 1
                if len(self.json_array_paths) < 6:
                    self.json_array_paths[format_path(schema) if schema else VALUE_NAME] = None
                return None
            table = self.children[schema] = self._new_table(_table_name(schema), schema, parent)
        return table

    def _add_column(self, table: _SqlTable, text: str) -> int:
        return self._alter(table, _unique_name(table.names, _sql_name(text)))

    def _alter(self, table: _SqlTable, name: str) -> int:
        self.conn.execute(f"ALTER TABLE {_q(table.name)} ADD COLUMN {_q(name)}")
        pos = table.width
        table.width += 1
        table.sql = None
        return pos

    def _value(self, v: Any) -> Any:
        """The SQLite value of a scalar (module docstring: numbers stay numbers)."""
        tv = type(v)
        if tv is str or tv is float or v is None:
            return v
        if tv is int:
            if _INT64_MIN <= v <= _INT64_MAX:
                return v
            self.numbers_as_text += 1
            return int.__repr__(v)
        if tv is bool:
            return 1 if v else 0
        if tv is JsonFloat:
            f = _exact_double(v)
            if f is not None:
                return f
            self.numbers_as_text += 1
            return v.raw
        if tv is JsonInt:
            if _INT64_MIN <= v <= _INT64_MAX:
                return int(v)                   # "-0"
            self.numbers_as_text += 1
            return v.raw
        return codec.number_text(v)  # pragma: no cover - Decimal

    def _set(self, table: _SqlTable, row: list[Any], rel: tuple, v: Any, extras: dict | None,
             as_json: bool = False) -> dict | None:
        """Store ``v`` (a scalar; a container with ``as_json``) at ``rel``; a value that
        finds no column (the table is full) goes into ``extras``, which is returned."""
        pos = table.cols.get(rel)
        if pos is None:
            if table.width >= self.max_columns - 1:     # the last column is kept for _extra
                if extras is None:
                    extras = {}
                extras[format_path(rel) if rel else VALUE_NAME] = v
                return extras
            pos = table.cols[rel] = self._add_column(table, format_path(rel) if rel else VALUE_NAME)
        n = len(row)
        if pos >= n:
            row.extend([None] * (pos + 1 - n))
        if as_json:
            value = _json_text(v)
            if self.check_surrogates and _SURROGATE_RE.search(value):  # pragma: no cover - escaped by dumps
                value = _escape_surrogates(value)
        else:
            if self.check_surrogates and type(v) is str and not v.isascii() and _SURROGATE_RE.search(v):
                v = _escape_surrogates(v)
            value = self._value(v)
        row[pos] = value
        return extras

    def _fill(self, table: _SqlTable, row: list[Any], value: Any, row_id: int, pending: deque) -> dict | None:
        """Scalar leaves of ``value`` into ``row``; arrays are queued for their tables
        (JSON text beyond SQLITE_MAX_TABLES); values deeper than MAX_DEPTH become JSON
        text; the pooled keys of maps one JSON text. Returns the leaves without column."""
        tv = type(value)
        if tv is list:
            if table.depth >= MAX_DEPTH:
                self.deep_values += 1
                return self._set(table, row, (), value, None, as_json=True)
            child = self._table_for(table, ())
            if child is None:
                return self._set(table, row, (), value, None, as_json=True)
            pending.append((child, value, row_id))
            return None
        if tv is not dict:
            return self._set(table, row, (), value, None)
        extras: dict | None = None
        base = table.depth
        plans = self.plans
        prefix = table.prefix
        stack: list[tuple[tuple, Any]] = [((), value)]
        while stack:
            rel, v = stack.pop()
            tv = type(v)
            if tv is dict:
                if v and base + len(rel) >= MAX_DEPTH:
                    self.deep_values += 1
                    extras = self._set(table, row, rel, v, extras, as_json=True)
                    continue
                plan = plans.get(prefix + rel) if plans else None
                if plan is not None:
                    keep = plan.kept
                    if not keep:                    # every key is pooled: the object is one JSON text
                        if v:
                            extras = self._set(table, row, rel, v, extras, as_json=True)
                        continue
                    rest = {k: x for k, x in v.items() if k not in keep}
                    if rest:
                        stack.append((rel + (POOL_SEG,), _Pooled(rest)))
                    items = [(k, x) for k, x in v.items() if k in keep]
                else:
                    items = list(v.items())
                for k in range(len(items) - 1, -1, -1):
                    key, child = items[k]
                    stack.append((rel + (key,), child))
            elif tv is list:
                if base + len(rel) >= MAX_DEPTH:
                    self.deep_values += 1
                    extras = self._set(table, row, rel, v, extras, as_json=True)
                else:
                    child = self._table_for(table, rel)
                    if child is None:
                        extras = self._set(table, row, rel, v, extras, as_json=True)
                    else:
                        pending.append((child, v, row_id))
            elif tv is _Pooled:
                extras = self._set(table, row, rel, v.value, extras, as_json=True)
            else:
                extras = self._set(table, row, rel, v, extras)
        return extras

    def _finish_row(self, table: _SqlTable, row: list[Any], extras: dict | None) -> None:
        if extras:
            pos = table.extra_pos
            if pos is None:
                table.extra_name = _unique_name(table.names, EXTRA_NAME)
                pos = table.extra_pos = self._alter(table, table.extra_name)
            n = len(row)
            if pos >= n:
                row.extend([None] * (pos + 1 - n))
            row[pos] = codec.dumps_transient(extras).decode("utf-8")
            table.extra_rows += 1
        table.pending.append(row)
        table.rows += 1
        if len(table.pending) >= SQLITE_BATCH or len(table.pending) * table.width >= self.cell_budget:
            self._flush(table)

    def _flush(self, table: _SqlTable) -> None:
        rows = table.pending
        if not rows:
            return
        width = table.width
        for r in rows:
            if len(r) < width:
                r.extend([None] * (width - len(r)))
        if table.sql is None:
            table.sql = f"INSERT INTO {_q(table.name)} VALUES ({','.join('?' * width)})"
        self.conn.executemany(table.sql, rows)
        self.rows += len(rows)
        table.pending = []

    def add(self, index: int, key: str | None, value: Any, order: int = 0) -> None:
        if key is not None and self.check_surrogates and _SURROGATE_RE.search(key):
            key = _escape_surrogates(key)
        row: list[Any] = [index]
        if self.ordered:
            row.append(order)
        if self.member:
            row.append(key)
        pending: deque = deque()
        extras = self._fill(self.records, row, value, index, pending)
        self._finish_row(self.records, row, extras)
        while pending:
            child, items, parent_id = pending.popleft()
            for k, item in enumerate(items):
                cid = child.next_id
                child.next_id += 1
                crow: list[Any] = [cid, parent_id, k]
                ti = type(item)
                if ti is dict:
                    extras = self._fill(child, crow, item, cid, pending)
                elif ti is list:
                    extras = None
                    if child.depth >= MAX_DEPTH:
                        self.deep_values += 1
                        extras = self._set(child, crow, (), item, None, as_json=True)
                    else:
                        sub = self._table_for(child, ())
                        if sub is None:
                            extras = self._set(child, crow, (), item, None, as_json=True)
                        else:
                            pending.append((sub, item, cid))
                else:
                    extras = self._set(child, crow, (), item, None)
                self._finish_row(child, crow, extras)

    def commit(self) -> None:
        for table in self.order:
            self._flush(table)
        self.conn.execute("COMMIT")
        self.conn.execute("BEGIN")

    def finish(self, wrapper: tuple[str, str] | None = None) -> None:
        """The last rows, an index on every ``_parent``, the ``_jsonreshape`` table and -- given
        ``wrapper`` = ``(record path, JSON text)`` -- the ``_wrapper`` table; then COMMIT."""
        for table in self.order:
            self._flush(table)
        for table in self.order:
            if table.child:
                check(self.ctx.cancel)
                index = _unique_name(self.names, _sql_name(table.name + "._parent"))
                self.conn.execute(f'CREATE INDEX {_q(index)} ON {_q(table.name)} ("_parent")')
        self._describe()
        if wrapper is not None:
            self.conn.execute(f'CREATE TABLE {_q(WRAPPER_TABLE)} ("path" TEXT, "json" TEXT)')
            self.conn.execute(f"INSERT INTO {_q(WRAPPER_TABLE)} VALUES (?, ?)", wrapper)
        self.conn.execute("COMMIT")

    def _describe(self) -> None:
        """The ``_jsonreshape`` table: one row per data table -- ``name``, ``path`` (the JSON path
        of its rows inside a record: ``$`` for the records, ``orders[*]``, ``orders[*].items[*]``,
        ``[*]`` for the elements of array records), ``parent`` (the table holding the parent rows,
        NULL for ``records``), ``kind`` (``records`` / ``array``) and ``rows``."""
        self.conn.execute(f'CREATE TABLE {_q(META_TABLE)} ("name" TEXT, "path" TEXT, "parent" TEXT, "kind" TEXT, '
                          '"rows" INTEGER)')
        rows = []
        for table in self.order:
            path = format_path(table.prefix) if table.child else RECORD_COLUMN
            if _SURROGATE_RE.search(path):
                path = _escape_surrogates(path)
            rows.append((table.name, path, table.parent, "array" if table.child else "records", table.rows))
        self.conn.executemany(f"INSERT INTO {_q(META_TABLE)} VALUES (?, ?, ?, ?, ?)", rows)


def _sqlite_scan(ctx: _Ctx) -> _SqliteScan:
    """The first pass of a SQLite export (map detection); huge records and records that
    cannot be decoded in memory are left out, as by the export."""
    scan = _SqliteScan()
    member, split = ctx.member, ctx.doc.split_member
    add = scan.add
    ctx.message("writer.progress.columns")
    for blk in ctx.blocks():
        if not blk.huge:
            sp = _pairs(blk.spans)
            data = blk.data
            for k in range(len(blk.indices)):
                if k & (CHECK_EVERY - 1) == CHECK_EVERY - 1:
                    check(ctx.cancel)
                raw = data[sp[2 * k]:sp[2 * k + 1]]
                if member:
                    raw = raw[split(raw)[1]:]
                try:
                    value = _scan_loads(raw)
                except _DECODE_ERRORS:
                    continue
                add(value)
        ctx.advance(blk)
    ctx.next_pass()
    ctx.message("writer.progress.export")
    return scan


def _ascending_source(src: Any) -> bool:
    """The selection is in file order (no ``_ord`` column needed): all records, an ascending
    ``range``, an ascending array/list, a RecordSet that says it is ascending."""
    if src is None:
        return True
    if isinstance(src, range):
        return src.step > 0 or len(src) <= 1
    if isinstance(src, (array, list, tuple)):
        return len(src) <= 1 or all(map(operator.lt, src, itertools.islice(src, 1, None)))
    is_asc = getattr(src, "is_ascending", None)
    if callable(is_asc):
        try:
            return bool(is_asc())
        except Exception:  # pragma: no cover - a source that cannot tell
            return False
    return False


def _wrapper_json(ctx: _Ctx) -> tuple[str, str] | None:
    """``(record path, compact JSON text of the wrapper with null in place of the record
    container)`` of a wrapped document (the SQLite ``_wrapper`` table); ``None`` for other kinds,
    a partial document or a wrapper larger than ``Settings.large_node_bytes``."""
    doc = ctx.doc
    if doc.kind != "wrapped" or doc.error is not None:
        return None
    spans = doc.wrapper_spans(cancel=ctx.cancel)
    if spans is None:
        return None
    (ps, pe), (ss, se) = spans
    if (pe - ps) + (se - ss) > doc.settings.large_node_bytes:
        return None
    rf = core.Reformatter(None)
    try:
        parts = [rf.feed(c) for c in ctx.chunks(ps, pe)]
        parts.append(rf.feed(b"null"))
        parts.extend(rf.feed(c) for c in ctx.chunks(ss, se))
        parts.append(rf.finish())
    except ValueError:  # pragma: no cover - the index validated the wrapper
        log.warning("the wrapper of %s could not be reformatted", doc.source_path, exc_info=True)
        return None
    text = b"".join(parts).decode("utf-8", "replace")
    return doc.info.record_path_text or "$", text


def _export_sqlite(ctx: _Ctx, out: Path) -> list[Path]:
    doc = ctx.doc
    # a selection may repeat a record: a bitmap of the records written (1 bit each)
    seen = bytearray((doc.record_count + 7) >> 3) if ctx.src is not None else None
    duplicates = 0
    member = ctx.member
    split = doc.split_member
    ordered = not _ascending_source(ctx.src)
    wrapper = _wrapper_json(ctx) if doc.kind == "wrapped" and ctx.opts.keep_wrapper else None
    if doc.kind == "wrapped" and ctx.opts.keep_wrapper and wrapper is None:
        ctx.wrapper_not_written()
    plans = _sqlite_scan(ctx).plans()
    with atomic_path(out) as tmp:
        conn = sqlite3.connect(long_path(tmp), isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA cache_size=-32768")
            conn.execute("BEGIN")
            writer = _SqliteWriter(conn, ctx, plans, ordered)
            since_commit = 0
            for blk in ctx.blocks():
                if blk.huge:
                    ctx.skip_huge("writer.warn.huge_skipped_sqlite")
                else:
                    sp = _pairs(blk.spans)
                    data = blk.data
                    idx = blk.indices
                    for k in range(len(idx)):
                        if k & (CHECK_EVERY - 1) == CHECK_EVERY - 1:
                            check(ctx.cancel)
                        index = idx[k]
                        if seen is not None:
                            byte, bit = index >> 3, 1 << (index & 7)
                            if seen[byte] & bit:
                                duplicates += 1
                                continue
                            seen[byte] |= bit
                        try:
                            key, value = _decode(data[sp[2 * k]:sp[2 * k + 1]], member, split)
                        except _DECODE_ERRORS:
                            ctx.skip_deep("writer.warn.deep_skipped_sqlite")
                            continue
                        writer.add(index, key, value, ctx.records)
                        ctx.records += 1
                    since_commit += len(blk.data)
                ctx.advance(blk)
                if since_commit >= SQLITE_COMMIT_BYTES:
                    writer.commit()
                    since_commit = 0
            ctx.message("writer.progress.finish")
            writer.finish(wrapper)
        except sqlite3.Error as exc:
            raise ExportError("writer.error.sqlite", detail=str(exc)) from exc
        finally:
            conn.close()
        ctx.commit_check()
    if duplicates:
        ctx.warn("writer.warn.duplicates", add=duplicates)
    if plans:
        ctx.warn("writer.warn.map_columns", add=len(plans), limit=MAP_MIN_KEYS, max=MAP_KEEP_MAX,
                 paths=_names_text([_pool_text(pl) for pl in plans.values()]))
    if writer.numbers_as_text:
        ctx.warn("writer.warn.sqlite_number_text", add=writer.numbers_as_text)
    extra = [tb for tb in writer.order if tb.extra_rows]
    if extra:
        ctx.warn("writer.warn.sqlite_extra", add=sum(tb.extra_rows for tb in extra), max=writer.max_columns,
                 tables=_names_text([tb.name for tb in extra]),
                 column=", ".join(f'"{n}"' for n in dict.fromkeys(tb.extra_name for tb in extra)))
    if writer.json_arrays:
        ctx.warn("writer.warn.sqlite_tables", add=writer.json_arrays, max=SQLITE_MAX_TABLES,
                 paths=_names_text(list(writer.json_array_paths)))
    if writer.deep_values:
        ctx.warn("writer.warn.sqlite_deep", add=writer.deep_values, limit=MAX_DEPTH)
    ctx.rows = writer.rows
    ctx.tables = [tb.name for tb in writer.order]
    return [out]


def _export_parquet(ctx: _Ctx, out: Path) -> list[Path]:
    """Parquet (stage 7): :func:`jsonreshape.writer_parquet.export_parquet` (imported here: pyarrow is optional)."""
    from .writer_parquet import export_parquet

    return export_parquet(ctx, out)


_EXPORTERS: dict[str, Callable[[_Ctx, Path], list[Path]]] = {
    "json": _export_json,
    "jsonl": _export_jsonl,
    "csv": _export_csv,
    "xlsx": _export_xlsx,
    "sqlite": _export_sqlite,
    "parquet": _export_parquet,
}
