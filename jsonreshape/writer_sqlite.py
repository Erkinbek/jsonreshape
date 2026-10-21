r"""SQLite export in worker processes (docs/ARCHITECTURE.md §8; stage 7): ``fmt="sqlite"`` of
:func:`jsonreshape.writer.export`. The tables, columns, ids, links, number rules, limits and
warnings are exactly those of :mod:`jsonreshape.writer` ("SQLite"): the database is value for
value the one the one-thread exporter of stages 1-6 wrote (``writer._export_sqlite_onethread``,
kept as the reference the tests compare against and as the fallback where the ``sqlite3``
module cannot ``serialize`` a database). What changed is who does the work and when:

One optimistic pass instead of two
==================================
The one-thread export scanned every record first (map detection: the rare keys of a map-like
object are pooled as one JSON text, ``writer.warn.map_columns``) and then split the records.
Maps are rare, so this export **splits the records at once** and has every job also report, per
object path, the keys it saw and the widest object (counted in the job's walk, ``key_stats`` of
:class:`_LocalRows`; :class:`_KeyStats` is the same count for the records the calling thread does
itself); the calling thread merges those statistics (:class:`_MapGuard`). A path can only ever
become a map in the one-thread scan when it collects more than ``writer.MAP_MIN_KEYS`` different
keys, so as long as no merged path has more than that many keys (and there are no more paths than
``writer.SCAN_MAX_PATHS``), the decision "no map anywhere" is **exactly** the one-thread scan's.
When a path crosses the threshold (a possible map, or merely a wide object) the attempt is
abandoned -- its temporary database removed, the counters and warnings of the attempt dropped, the
workers stopped -- and the export is done again the old way: the one-thread scan
(``_sqlite_scan``, the same code and therefore the same plans) followed by the rows pass with
those plans in the workers (progress then grows by the two passes: ``writer.progress.columns``,
then the rows). Ordinary data never pays for the scan.

Rows in worker processes
========================
* The calling thread reads the blocks (``writer.POOL_BLOCK_BYTES``, the CSV rule for the
  number of workers -- ``export(..., workers=None)`` automatic, ``1`` this thread only;
  ``ExportReport.workers``), keeps at most ``2 * workers`` jobs in flight and takes the results
  in block order. A job (:class:`_SqliteJob`) decodes its records and splits them with the row
  engine of :class:`writer._SqliteWriter` (``add``, ``_set``, the number rules) whose object walk
  :meth:`_LocalRows._walk` does exactly what ``_SqliteWriter._fill`` does -- the same leaves, the
  same first-seen order of tables and columns, the same ``_id`` / ``_parent`` / ``_idx`` sequence
  inside the block; recursive, with the common scalars stored inline (the tests compare it with
  the one-thread code) -- into **local** tables (``t0``, ``t1``, ... with columns ``c0``, ``c1``,
  ..., the leaf paths kept beside them) of an in-memory SQLite database. A local table is created
  with all its columns when its first batch of rows (``writer.SQLITE_BATCH``) is inserted (an
  ``ALTER TABLE`` makes SQLite parse the whole schema again: map-like data would be quadratic),
  so a job holds at most one batch per table in Python. The job serializes the database
  (``Connection.serialize``) and returns it with a manifest: per local table its array path, its
  parent table, its leaf paths in column order and its rows. Child ids are 1, 2, ... **per table
  per segment** and nested ``_parent`` links point at those; the ``_ord`` of a records row is its
  number inside the segment.
* The calling thread (:class:`_SqliteTarget`, the one-thread writer's registry and connection)
  merges a segment: it first checks that every local table and column has, or can get, a global
  one (``SQLITE_MAX_TABLES`` child tables, ``SQLITE_MAX_COLUMNS`` columns -- the ``_extra``
  overflow column and arrays stored as JSON text are the one-thread code's business), creates the
  new tables and columns in the segment's first-seen order (``_table_name``, ``_unique_name``, the
  ``ALTER TABLE`` of the one-thread writer), loads the image into the attached in-memory database
  ``w`` (``Connection.deserialize``, after giving it a schema cookie of its own: SQLite would keep
  the parsed schema of the previous image, whose tables may sit on other root pages) and copies
  every table with one ``INSERT INTO main."t" (columns...) SELECT c0 + offset, c1 + parent offset,
  ... FROM w.tN`` -- the offsets are the global row counts of the tables before the segment, so the
  ids are exactly the one-thread writer's; the columns are matched by leaf path, the values keep
  their storage class. No row passes through Python in the calling thread.
* What a job leaves to the calling thread is done there with the one-thread code
  (``_SqliteWriter.add``), in its place: records with more than ``writer.POOL_SAFE_BRACKETS``
  brackets (a job cuts its segment before such a record), a block that would need more columns
  in a table or more child tables than allowed (``("wide",)``: the whole block -- no merge could
  take it), a segment that the registry cannot take without the overflow column or the table cap
  (the segment's records), a job a worker could not do (``("redo",)``) and every block once the
  workers broke. Records that cannot be decoded in memory are skipped
  (``writer.warn.deep_skipped_sqlite``), huge records too (``writer.warn.huge_skipped_sqlite``);
  a record selected twice is written once (``writer.warn.duplicates``: the calling thread tells a
  job which positions to skip).
* ``workers=1`` runs the same jobs in the calling thread (``writer._NoPool``).
* The database: ``PRAGMA page_size = 32768``, ``journal_mode = OFF``, ``synchronous = OFF``
  (inside the temporary file of ``atomic_path``: it is renamed over the target only when
  complete), a 16 MiB page cache while the rows are appended, one transaction committed every
  ``writer.SQLITE_COMMIT_BYTES`` of source; the indexes on every ``_parent`` (a 64 MiB cache for
  the sorter: the workers have exited by then), the ``_jsonreshape`` and ``_wrapper`` tables at the
  end (``writer.progress.finish``), as before.
* Memory: per worker ~40 MB of interpreter and modules plus one block, its rows (one batch per
  table) and its image (~0.6 MB per MiB of records); in the calling thread up to ``2 * workers``
  blocks and images, the registry and SQLite's cache -- measured on ``bench/data/syn_2g.json``
  with 4 workers: ~265 MB in all. Progress by the bytes read; ``cancel`` is checked for every
  block, every job result and before the commit. An error or a cancellation leaves no temporary
  file and an existing target untouched.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .errors import ExportError
from .fileio import atomic_path, long_path
from .paths import Wild, format_path
from .tasks import check
from . import writer as _w   # (the tunables and the pool, read at call time: tests patch them there)
from .writer import (
    FORMAT_NAMES,
    MAX_DEPTH,
    POOL_SEG,
    VALUE_NAME,
    _DECODE_ERRORS,
    _INT64_MAX,
    _INT64_MIN,
    _ascending_source,
    _codec_fast,
    _Ctx,
    _decode,
    _export_sqlite_onethread,
    _MapPlan,
    _NoPool,
    _pairs,
    _pool_workers,
    _q,
    _sqlite_scan,
    _sqlite_warnings,
    _SqliteWriter,
    _SqlTable,
    _too_deep_maybe,
    _wrapper_json,
)

__all__ = ["export_sqlite"]

log = logging.getLogger(__name__)

PAGE_SIZE = 32768                 # PRAGMA page_size of the output database (fewer pages to manage than 4096)
CACHE_PAGES = -16384              # PRAGMA cache_size (KiB, negative) while the rows are merged: 16 MiB (appends)
FINISH_CACHE_PAGES = -65536       # ... and for the indexes at the end (the sorter's memory; the workers are gone)
JOB_BATCH = 1024                  # a job's rows per table inserted into its in-memory database at a time


class _Wide(Exception):
    """A job's table would need more columns than a table may have: the calling thread does the block."""


class _Redo(Exception):
    """A segment the registry cannot take as it is (the table cap, the overflow column): its records are done
    by the calling thread."""


class _Restart(Exception):
    """A path may be a map: the export starts over with the one-thread scan first."""


# ----------------------------------------------------------------------------------
# the map guard
# ----------------------------------------------------------------------------------
class _KeyStats:
    """The keys seen at every object path (array elements ``[*]``), as the one-thread scan walks them
    (``writer._SqliteScan.add``: objects and arrays, ``MAX_DEPTH`` levels): ``path -> [keys, widest]`` -- for
    the records the calling thread does itself (a job counts the same in :meth:`_LocalRows._walk`; the tests
    compare both with the scan)."""

    __slots__ = ("nodes",)

    def __init__(self) -> None:
        self.nodes: dict[tuple, list] = {}

    def add(self, value: Any) -> None:
        nodes = self.nodes
        stack: list[tuple[tuple, Any]] = [((), value)]
        while stack:
            path, v = stack.pop()
            tv = type(v)
            if tv is dict:
                node = nodes.get(path)
                if node is None:
                    node = nodes[path] = [set(), 0]
                node[0].update(v)
                if len(v) > node[1]:
                    node[1] = len(v)
                if len(path) + 1 < MAX_DEPTH:
                    stack.extend((path + (k,), x) for k, x in v.items() if type(x) is dict or type(x) is list)
            elif tv is list and len(path) + 1 < MAX_DEPTH:
                p = path + (Wild.ANY_INDEX,)
                stack.extend((p, x) for x in v if type(x) is dict or type(x) is list)

    def summary(self) -> list[tuple[tuple, tuple, int]]:
        return [(path, tuple(node[0]), node[1]) for path, node in self.nodes.items()]


class _MapGuard:
    """The merged key statistics of an optimistic attempt: :meth:`merge` / :meth:`add_value` return ``True``
    once a path may be a map in the one-thread scan (more than ``MAP_MIN_KEYS`` keys) or more paths than it
    tracks were seen -- the attempt must then be redone the exact way (module docstring)."""

    def __init__(self) -> None:
        self.nodes: dict[tuple, list] = {}

    def merge(self, summary: list[tuple[tuple, tuple, int]]) -> bool:
        nodes = self.nodes
        for path, keys, widest in summary:
            node = nodes.get(path)
            if node is None:
                node = nodes[path] = [set(), 0]
            node[0].update(keys)
            if widest > node[1]:
                node[1] = widest
            if len(node[0]) > _w.MAP_MIN_KEYS:
                return True
        return len(nodes) > _w.SCAN_MAX_PATHS

    def add_value(self, value: Any) -> bool:
        stats = _KeyStats()
        stats.add(value)
        return self.merge(stats.summary())


# ----------------------------------------------------------------------------------
# the worker side: records -> local tables -> a serialized database
# ----------------------------------------------------------------------------------
class _LocalRows(_SqliteWriter):
    """The row engine of :class:`writer._SqliteWriter` on an in-memory database: tables ``t0``, ``t1``, ... in
    first-seen order, columns by position (``c0``, ...; the leaf paths in ``cols``), rows inserted in the
    writer's batches (so a job holds at most ``SQLITE_BATCH`` rows per table in Python). A leaf beyond the column
    limit or more than ``SQLITE_MAX_TABLES`` child tables raise :class:`_Wide` (the one-thread writer's
    ``_extra`` overflow and JSON-text arrays are the calling thread's: such a block could never be merged).
    ``key_stats``: the keys of the objects walked (the map guard's statistics; ``None`` in the exact pass)."""

    def __init__(self, member: bool, check_surrogates: bool, cell_budget: int, plans: dict[tuple, _MapPlan] | None,
                 ordered: bool, max_columns: int, key_stats: dict[tuple, list] | None) -> None:
        ctx = SimpleNamespace(member=member, check_surrogates=check_surrogates, cell_budget=cell_budget, cancel=None)
        self.created: dict[str, int] = {}     # local table -> the columns it has in the database so far
        conn = sqlite3.connect(":memory:", isolation_level=None)
        try:
            conn.execute("BEGIN")
            super().__init__(conn, ctx, plans, ordered)   # type: ignore[arg-type]
        except BaseException:
            conn.close()
            raise
        self.max_columns = max_columns
        self.key_stats = key_stats

    def _new_table(self, base: str, schema: tuple | None, parent: _SqlTable | None = None) -> _SqlTable:
        # (no SQL yet: the table is created with all its columns when its first rows are inserted -- every
        # ALTER TABLE makes SQLite parse the whole schema again)
        if schema is not None:
            nfixed = 3                                                    # _id, _parent, _idx
        else:
            nfixed = 1 + int(self.ordered) + int(self.member)             # _id, (_ord), ($key)
        table = _SqlTable(f"t{len(self.order)}", schema, [f"c{i}" for i in range(nfixed)],
                          parent.name if parent is not None else None)
        self.order.append(table)
        return table

    def _table_for(self, parent: _SqlTable, rel: tuple) -> _SqlTable | None:
        schema = parent.prefix + rel
        table = self.children.get(schema)
        if table is None:
            if len(self.children) >= _w.SQLITE_MAX_TABLES:   # (no merge could take it: the calling thread's)
                raise _Wide()
            table = self.children[schema] = self._new_table("", schema, parent)
        return table

    def _add_column(self, table: _SqlTable, text: str) -> int:
        return self._alter(table, f"c{table.width}")

    def _alter(self, table: _SqlTable, name: str) -> int:
        pos = table.width
        table.width += 1
        table.colnames.append(name)
        table.sql = None
        return pos

    def _finish_row(self, table: _SqlTable, row: list[Any], extras: dict | None) -> None:
        table.pending.append(row)        # (extras: never -- a leaf without column raised _Wide)
        table.rows += 1
        n = len(table.pending)
        if n >= JOB_BATCH or n >= _w.SQLITE_BATCH or n * table.width >= self.cell_budget:
            self._flush(table)

    def _flush(self, table: _SqlTable) -> None:
        if not table.pending:
            return
        width = table.width
        have = self.created.get(table.name)
        if have is None:
            self.conn.execute(f"CREATE TABLE {table.name} ({','.join(f'c{i}' for i in range(width))})")
        else:
            for i in range(have, width):          # (columns found after the table's first rows)
                self.conn.execute(f"ALTER TABLE {table.name} ADD COLUMN c{i}")
        self.created[table.name] = width
        super()._flush(table)

    def _overflow(self, table: _SqlTable, rel: tuple, v: Any, extras: dict | None) -> dict | None:
        raise _Wide()

    def _fill(self, table: _SqlTable, row: list[Any], value: Any, row_id: int, pending: deque) -> dict | None:
        if type(value) is not dict:
            return super()._fill(table, row, value, row_id, pending)   # (scalars and arrays: the writer's code)
        return self._walk(table, row, (), value, None, row_id, pending)

    def _walk(self, table: _SqlTable, row: list[Any], rel: tuple, obj: dict, extras: dict | None, row_id: int,
              pending: deque) -> dict | None:
        """The object ``obj`` at ``rel`` in a row of ``table``: exactly what ``_SqliteWriter._fill`` does with it
        (pre-order, keys in their order, the pooled keys of a map last, the same depth rules) -- recursive instead
        of its explicit stack, the common scalars stored inline (``_set`` + ``_value`` for a known column) -- and
        its keys counted in :attr:`key_stats` (the paths and depths ``writer._SqliteScan.add`` walks)."""
        depth = table.depth + len(rel)
        if obj and depth >= MAX_DEPTH:
            self.deep_values += 1
            return self._set(table, row, rel, obj, extras, as_json=True)
        prefix = table.prefix
        stats = self.key_stats
        if stats is not None and depth < MAX_DEPTH:
            node = stats.get(prefix + rel)
            if node is None:
                stats[prefix + rel] = [set(obj), len(obj)]
            else:
                node[0].update(obj)
                if len(obj) > node[1]:
                    node[1] = len(obj)
        items: Any = obj.items()
        rest: dict | None = None
        plans = self.plans
        if plans:
            plan = plans.get(prefix + rel)
            if plan is not None:
                keep = plan.kept
                if not keep:                                # every key is pooled: the object is one JSON text
                    if obj:
                        extras = self._set(table, row, rel, obj, extras, as_json=True)
                    return extras
                items = [(k, x) for k, x in items if k in keep]
                rest = {k: x for k, x in obj.items() if k not in keep}
        cols = table.cols
        any_text = not self.check_surrogates               # (else non-ASCII texts through _set: lone surrogates)
        for key, x in items:
            r = rel + (key,)
            tx = type(x)
            if tx is dict:
                extras = self._walk(table, row, r, x, extras, row_id, pending)
            elif tx is list:
                if depth + 1 >= MAX_DEPTH:
                    self.deep_values += 1
                    extras = self._set(table, row, r, x, extras, as_json=True)
                else:
                    child = self._table_for(table, r)
                    if child is None:  # pragma: no cover - a job creates every table (the cap is the merge's)
                        extras = self._set(table, row, r, x, extras, as_json=True)
                    else:
                        pending.append((child, x, row_id))
            else:
                pos = cols.get(r)
                if pos is not None and pos < len(row):
                    if tx is str:
                        if any_text or x.isascii():
                            row[pos] = x
                            continue
                    elif tx is float or x is None:
                        row[pos] = x
                        continue
                    elif tx is int:
                        if _INT64_MIN <= x <= _INT64_MAX:
                            row[pos] = x
                            continue
                    elif tx is bool:
                        row[pos] = 1 if x else 0
                        continue
                extras = self._set(table, row, r, x, extras)
        if rest:
            extras = self._set(table, row, rel + (POOL_SEG,), rest, extras, as_json=True)
        return extras

    def segment(self, records: int, k_start: int, k_end: int, skipped: int) -> tuple:
        """The segment of :meth:`_SqliteJob.run` (the database serialized; the connection is closed)."""
        manifest = []
        try:
            for table in self.order:
                self._flush(table)
                nfixed = len(table.colnames) - len(table.cols)
                leaves: list[Any] = [None] * len(table.cols)
                for rel, pos in table.cols.items():
                    leaves[pos - nfixed] = rel
                parent = int(table.parent[1:]) if table.parent is not None else None
                manifest.append((table.schema, parent, leaves, table.rows))
            self.conn.execute("COMMIT")
            blob = self.conn.serialize()
        finally:
            self.close()
        return (blob, manifest, records, k_start, k_end, skipped, self.numbers_as_text, self.deep_values)

    def close(self) -> None:
        self.conn.close()


class _SqliteJob:
    """A block of records split in a worker process (module docstring, "Rows in worker processes").

    :meth:`run` returns ``("ok", pieces, summary)``: ``pieces`` -- in order, segments ``(blob, manifest,
    records, k_start, k_end, skipped, numbers_as_text, deep_values)`` (a serialized database of the rows of the
    records ``k_start .. k_end - 1`` -- ``None`` when none could be decoded -- and its manifest ``[(array path
    or None, parent's local index, leaf paths by column, rows), ...]``, the records in it, those of the range
    that cannot be decoded in memory, the two counters of the row engine) and the numbers ``k`` of the records
    the calling thread does itself; ``summary`` -- the key statistics (``None`` with plans: the exact pass).
    ``("wide",)``: the calling thread does the block."""

    def __init__(self, member: bool, check_surrogates: bool, cell_budget: int, plans: dict[tuple, _MapPlan] | None,
                 ordered: bool, max_columns: int) -> None:
        self.member = member
        self.check_surrogates = check_surrogates
        self.cell_budget = cell_budget
        self.plans = plans
        self.ordered = ordered
        self.max_columns = max_columns
        self.split: Callable[[bytes], tuple[str, int]] | None = None
        if member:
            from .reader import split_member

            self.split = split_member

    def _engine(self, key_stats: dict[tuple, list] | None) -> _LocalRows:
        return _LocalRows(self.member, self.check_surrogates, self.cell_budget, self.plans, self.ordered,
                          self.max_columns, key_stats)

    def run(self, data: bytes, spans: bytes, indices: Any, skips: tuple) -> tuple:
        if type(indices) is bytes:
            indices = _pairs(indices)
        sp = _pairs(spans)
        n = len(indices)
        member, split = self.member, self.split
        key_stats: dict[tuple, list] | None = {} if self.plans is None else None
        skip = frozenset(skips)
        pieces: list[Any] = []
        rows: _LocalRows | None = None   # the engine of the segment being built (made at its first record) ...
        records = 0                      # ... its records ...
        skipped = 0                      # ... those of its range that cannot be decoded ...
        seg_start = 0                    # ... and its first record

        def cut(k_end: int) -> None:
            nonlocal rows, records, skipped, seg_start
            if rows is not None:
                pieces.append(rows.segment(records, seg_start, k_end, skipped))
                rows = None
            elif skipped:
                pieces.append((None, [], 0, seg_start, k_end, skipped, 0, 0))
            records = skipped = 0
            seg_start = k_end

        try:
            for k in range(n):
                if k in skip:
                    continue
                raw = data[sp[2 * k]:sp[2 * k + 1]]
                if _too_deep_maybe(raw):
                    cut(k)
                    pieces.append(k)
                    seg_start = k + 1
                    continue
                try:
                    key, value = _decode(raw, member, split)
                except _DECODE_ERRORS:
                    skipped += 1
                    continue
                if rows is None:
                    rows = self._engine(key_stats)
                try:
                    rows.add(int(indices[k]), key, value, records)
                except _Wide:
                    return ("wide",)
                records += 1
            cut(n)
        finally:
            if rows is not None:
                rows.close()
        summary = [(path, tuple(node[0]), node[1]) for path, node in key_stats.items()] if key_stats is not None \
            else None
        return ("ok", pieces, summary)


def _sqlite_pool_init(spec: tuple, fast: bool) -> None:
    """Worker process start: the job of the export and the caller's decoding."""
    _w._pool_init_codec(fast)
    _w._POOL_STATE["sqlite"] = _SqliteJob(*spec)


def _sqlite_pool_block(data: bytes, spans: bytes, indices: bytes, skips: tuple) -> tuple:
    """One job in a worker process, or ``("redo",)`` when anything failed (the calling thread does the block)."""
    try:
        return _w._POOL_STATE["sqlite"].run(data, spans, indices, skips)
    except Exception:  # noqa: BLE001 - redone (and raised) by the calling thread
        log.debug("a SQLite job failed in a worker process; the calling thread redoes it", exc_info=True)
        return ("redo",)


# ----------------------------------------------------------------------------------
# the calling thread: the registry, the merge of segments
# ----------------------------------------------------------------------------------
class _SqliteTarget(_SqliteWriter):
    """The one-thread writer (registry, connection, ``add`` for the records done here) plus :meth:`merge` of
    the segments the jobs return (module docstring)."""

    def attach(self) -> None:
        self.conn.execute("ATTACH DATABASE ':memory:' AS w")
        self.images = 0                       # segments loaded into "w"

    def _load(self, blob: bytes) -> None:
        """The segment's database as the attached ``w``. SQLite keeps the parsed schema of an attached database
        across ``deserialize`` while the schema cookie of the new image (header offset 40) equals the old one --
        and every job's image is a new database with the same cookie, whose tables may sit on other root pages
        (statements then read the wrong pages): each image gets a cookie of its own first."""
        self.images += 1
        image = bytearray(blob)
        image[40:44] = (1 + self.images % 0x7FFFFFFE).to_bytes(4, "big")
        self.conn.deserialize(image, name="w")

    def merge(self, blob: bytes, manifest: list[tuple], order_base: int) -> None:
        """Copy a segment into the database (ids offset, columns by leaf path); :class:`_Redo` -- before anything
        is changed -- when a local table or column cannot get a global one."""
        tables: list[_SqlTable | None] = []
        new_tables = 0
        limit = self.max_columns - 1          # (the last column is kept for _extra)
        for schema, _parent, leaves, _nrows in manifest:
            if schema is None:
                table: _SqlTable | None = self.records
            else:
                table = self.children.get(schema)
            if table is None:
                if len(self.children) + new_tables >= _w.SQLITE_MAX_TABLES or 3 + len(leaves) > limit:
                    raise _Redo()
                new_tables += 1
            else:
                cols = table.cols
                if table.width + sum(1 for rel in leaves if rel not in cols) > limit:
                    raise _Redo()
            tables.append(table)
        for i, (schema, parent_j, leaves, _nrows) in enumerate(manifest):
            table = tables[i]
            if table is None:
                parent = tables[parent_j]
                assert parent is not None and schema is not None
                table = self._table_for(parent, schema[len(parent.prefix):])
                assert table is not None
                tables[i] = table
            cols = table.cols
            for rel in leaves:
                if rel not in cols:
                    cols[rel] = self._add_column(table, format_path(rel) if rel else VALUE_NAME)
        conn = self.conn
        self._load(blob)
        offsets = [t.rows for t in tables]     # type: ignore[union-attr]
        for i, (schema, parent_j, leaves, nrows) in enumerate(manifest):
            if not nrows:
                continue
            table = tables[i]
            assert table is not None
            if table.child:
                parent = tables[parent_j]
                assert parent is not None
                sel = [f"c0+{offsets[i]}", f"c1+{offsets[parent_j]}" if parent.child else "c1", "c2"]
                nfixed = 3
            else:
                sel = ["c0"]
                if self.ordered:
                    sel.append(f"c1+{order_base}")
                if self.member:
                    sel.append(f"c{len(sel)}")
                nfixed = len(sel)
            names = list(table.colnames[:nfixed])
            cols = table.cols
            colnames = table.colnames
            for m, rel in enumerate(leaves):
                sel.append(f"c{nfixed + m}")
                names.append(colnames[cols[rel]])
            conn.execute(f"INSERT INTO main.{_q(table.name)} ({','.join(_q(c) for c in names)}) "
                         f"SELECT {','.join(sel)} FROM w.t{i}")
            table.rows += nrows
            self.rows += nrows
            if table.child:
                table.next_id += nrows


class _SqlitePool:
    """The blocks of the selection through the jobs (worker processes, or this thread with ``workers == 1``),
    the results merged in order; the records a job leaves to this thread done here (module docstring)."""

    def __init__(self, ctx: _Ctx, target: _SqliteTarget, workers: int, guard: _MapGuard | None,
                 seen: bytearray | None) -> None:
        self.ctx = ctx
        self.target = target
        self.workers = workers
        self.guard = guard
        self.seen = seen
        self.duplicates = 0
        self.inflight = max(2, 2 * workers)
        self.member = ctx.member
        self.split = ctx.doc.split_member
        self.spec = (ctx.member, ctx.check_surrogates, ctx.cell_budget, None if guard is not None else target.plans,
                     target.ordered, target.max_columns)
        self.pool: Any = None
        self.local: _SqliteJob | None = None
        self.waiting: deque = deque()
        self.since_commit = 0

    def run(self) -> None:
        ctx = self.ctx
        if self.workers > 1:
            pool = _w._Pool(self.workers, _sqlite_pool_init, (self.spec, _codec_fast()), ctx.cancel,
                         f"{FORMAT_NAMES['sqlite']} export")
        else:
            pool = _NoPool()
        self.pool = pool
        blocks = ctx.doc.iter_blocks(ctx.src, block_bytes=_w.POOL_BLOCK_BYTES, cancel=ctx.cancel)
        try:
            for blk in blocks:
                check(ctx.cancel)
                if blk.huge:
                    self._drain()
                    ctx.skip_huge("writer.warn.huge_skipped_sqlite")
                    self._done(blk)
                    continue
                self._submit(blk, self._skips(blk))
                while len(self.waiting) >= self.inflight:
                    self._take()
            self._drain()
        finally:
            blocks.close()
            waiting, self.waiting = self.waiting, deque()
            pool.close(fut for fut, _blk, _skips in waiting)
        ctx.workers = max(ctx.workers, 1 if pool.broken else self.workers)

    def _skips(self, blk: Any) -> tuple:
        """The positions of the block's records written before (a selection naming a record twice)."""
        seen = self.seen
        if seen is None:
            return ()
        out = []
        for k, index in enumerate(blk.indices):
            byte, bit = index >> 3, 1 << (index & 7)
            if seen[byte] & bit:
                out.append(k)
            else:
                seen[byte] |= bit
        if out:
            self.duplicates += len(out)
        return tuple(out)

    def _submit(self, blk: Any, skips: tuple) -> None:
        fut = self.pool.submit(_sqlite_pool_block, blk.data, blk.spans, blk.indices.tobytes(), skips)
        if fut is not None:
            self.waiting.append((fut, blk, skips))
            return
        self._drain()                     # (no workers, or they broke: the jobs in flight come first)
        self._run_job(blk, skips)         # the same job as a worker's, here

    def _run_job(self, blk: Any, skips: tuple) -> None:
        if self.local is None:
            self.local = _SqliteJob(*self.spec)
        res = self.local.run(blk.data, blk.spans, blk.indices, skips)
        if res[0] != "ok":
            self._run_block(blk, skips)
            return
        self._merge_result(blk, res, skips)

    def _drain(self) -> None:
        while self.waiting:
            self._take()

    def _take(self) -> None:
        fut, blk, skips = self.waiting.popleft()
        res = self.pool.result(fut)
        if res is None or res[0] != "ok":
            self._run_block(blk, skips)
            return
        self._merge_result(blk, res, skips)

    def _merge_result(self, blk: Any, res: tuple, skips: tuple) -> None:
        _tag, pieces, summary = res
        ctx, target = self.ctx, self.target
        if self.guard is not None and summary is not None and self.guard.merge(summary):
            raise _Restart()
        skip = frozenset(skips)
        for p in pieces:
            if type(p) is int:
                self._record(blk, p)
                continue
            blob, manifest, records, k_start, k_end, skipped, numbers_as_text, deep_values = p
            if blob is not None:
                try:
                    target.merge(blob, manifest, ctx.records)
                except _Redo:
                    for k in range(k_start, k_end):       # (the undecodable ones are counted again here)
                        if k not in skip:
                            self._record(blk, k)
                    continue
                ctx.records += records
                target.numbers_as_text += numbers_as_text
                target.deep_values += deep_values
            for _ in range(skipped):
                ctx.skip_deep("writer.warn.deep_skipped_sqlite")
        self._done(blk)

    def _run_block(self, blk: Any, skips: tuple) -> None:
        """The whole block record by record with the one-thread code."""
        skip = frozenset(skips)
        for k in range(len(blk.indices)):
            if k not in skip:
                self._record(blk, k)
        self._done(blk)

    def _record(self, blk: Any, k: int) -> None:
        """Record ``k`` of ``blk`` exactly as the one-thread export does it."""
        ctx, target = self.ctx, self.target
        s, e = blk.span(k)
        try:
            key, value = _decode(blk.data[s:e], self.member, self.split)
        except _DECODE_ERRORS:
            ctx.skip_deep("writer.warn.deep_skipped_sqlite")
            return
        if self.guard is not None and self.guard.add_value(value):
            raise _Restart()
        target.add(int(blk.indices[k]), key, value, ctx.records)
        ctx.records += 1

    def _done(self, blk: Any) -> None:
        ctx = self.ctx
        ctx.advance(blk)
        self.since_commit += len(blk.data)
        if self.since_commit >= _w.SQLITE_COMMIT_BYTES:
            self.target.commit()
            self.since_commit = 0


# ----------------------------------------------------------------------------------
# the export
# ----------------------------------------------------------------------------------
class _State:
    """The counters and warnings of ``ctx`` before an attempt (restored when it is redone)."""

    def __init__(self, ctx: _Ctx) -> None:
        self.records, self.rows, self.skipped = ctx.records, ctx.rows, ctx.skipped
        self.warnings = {k: dict(v) for k, v in ctx._warnings.items()}

    def restore(self, ctx: _Ctx) -> None:
        ctx.records, ctx.rows, ctx.skipped = self.records, self.rows, self.skipped
        ctx._warnings = {k: dict(v) for k, v in self.warnings.items()}


def _attempt(ctx: _Ctx, out: Path, plans: dict[tuple, _MapPlan] | None, wrapper: tuple[str, str] | None,
             ordered: bool) -> None:
    """One pass over the records into the database ``out`` (``plans`` given: the exact pass after the scan;
    ``None``: the optimistic pass, which raises :class:`_Restart` when a map may exist)."""
    doc = ctx.doc
    seen = bytearray((doc.record_count + 7) >> 3) if ctx.src is not None else None
    workers = _pool_workers(ctx)
    with atomic_path(out) as tmp:
        conn = sqlite3.connect(long_path(tmp), isolation_level=None)
        try:
            conn.execute(f"PRAGMA page_size={PAGE_SIZE}")
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute(f"PRAGMA cache_size={CACHE_PAGES}")
            target = _SqliteTarget(conn, ctx, plans, ordered)
            target.attach()
            conn.execute("BEGIN")
            pool = _SqlitePool(ctx, target, workers, None if plans is not None else _MapGuard(), seen)
            pool.run()                                    # (its workers have exited when it returns)
            ctx.message("writer.progress.finish")
            conn.execute(f"PRAGMA cache_size={FINISH_CACHE_PAGES}")
            target.finish(wrapper)
        except sqlite3.Error as exc:
            raise ExportError("writer.error.sqlite", detail=str(exc)) from exc
        finally:
            conn.close()
        ctx.commit_check()
    _sqlite_warnings(ctx, target, plans or {}, pool.duplicates)


def export_sqlite(ctx: _Ctx, out: Path) -> list[Path]:
    """Write the records of ``ctx`` as the SQLite database ``out`` (module docstring)."""
    if not hasattr(sqlite3.Connection, "serialize") or not hasattr(sqlite3.Connection, "deserialize"):
        return _export_sqlite_onethread(ctx, out)   # pragma: no cover - sqlite3 without SQLITE_ENABLE_DESERIALIZE
    doc = ctx.doc
    ordered = not _ascending_source(ctx.src)
    wrapper = _wrapper_json(ctx) if doc.kind == "wrapped" and ctx.opts.keep_wrapper else None
    if doc.kind == "wrapped" and ctx.opts.keep_wrapper and wrapper is None:
        ctx.wrapper_not_written()
    before = _State(ctx)
    try:
        _attempt(ctx, out, None, wrapper, ordered)
    except _Restart:
        # a map may exist: the attempt is dropped (its file, counters and warnings) and the export is done
        # the exact way -- the one-thread scan, then the rows with its plans
        before.restore(ctx)
        meter = ctx.meter
        meter.next_pass()
        if meter.progress is not None:
            meter.progress.set_total(meter.base + 2 * meter.unit)
        plans = _sqlite_scan(ctx).plans()
        _attempt(ctx, out, plans, wrapper, ordered)
    return [out]
