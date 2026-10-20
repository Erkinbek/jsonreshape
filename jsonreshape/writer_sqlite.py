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
object path, the keys it saw and the widest object (:class:`_KeyStats`); the calling thread merges
those statistics (:class:`_MapGuard`). A path can only ever become a map in the one-thread scan
when it collects more than ``writer.MAP_MIN_KEYS`` different keys (and the scan tracks at most
``writer.SCAN_MAX_PATHS`` paths), so as long as no merged path has more than that many keys and
there are no more paths than that, the decision "no map anywhere" is **exactly** the one-thread
scan's. When a path crosses the threshold (a possible map) the attempt is abandoned -- its
temporary database removed, the counters and warnings of the attempt dropped, the workers
stopped -- and the export is done again the old way: the one-thread scan (``_sqlite_scan``, the
same code and therefore the same plans) followed by the rows pass with those plans in the
workers (progress then grows by the two passes: ``writer.progress.columns``, then the rows).

Rows in worker processes
========================
* The calling thread reads the blocks (``writer.POOL_BLOCK_BYTES``, the CSV rule for the
  number of workers -- ``export(..., workers=None)`` automatic, ``1`` this thread only;
  ``ExportReport.workers``), keeps at most ``2 * workers`` jobs in flight and takes the results
  in block order. A job (:class:`_SqliteJob`) decodes its records and splits them with the row
  engine of :class:`writer._SqliteWriter` -- the same ``_fill`` walk, so the same leaves, the
  same first-seen order of tables and columns, the same ``_id`` / ``_parent`` / ``_idx`` sequence
  inside the block -- into **local** tables (``t0``, ``t1``, ... with columns ``c0``, ``c1``, ...,
  the leaf paths kept beside them) of an in-memory SQLite database that it serializes
  (``Connection.serialize``) and returns with a manifest: per local table its array path, its
  parent table, its leaf paths in column order and its rows. Child ids are 1, 2, ... **per table
  per segment** and nested ``_parent`` links point at those; the ``_ord`` of a records row is its
  number inside the segment.
* The calling thread (:class:`_SqliteTarget`, the one-thread writer's registry and connection)
  merges a segment: it first checks that every local table and column has, or can get, a global
  one (``SQLITE_MAX_TABLES`` child tables, ``SQLITE_MAX_COLUMNS`` columns -- the ``_extra``
  overflow column and arrays stored as JSON text are the one-thread code's business), creates the
  new tables and columns in the segment's first-seen order (``_table_name``, ``_unique_name``, the
  ``ALTER TABLE`` of the one-thread writer), loads the blob into the attached in-memory database
  ``w`` (``Connection.deserialize``) and copies every table with one ``INSERT INTO main."t"
  (columns...) SELECT c0 + offset, c1 + parent offset, ... FROM w.tN`` -- the offsets are the
  global row counts of the tables before the segment, so the ids are exactly the one-thread
  writer's; the columns are matched by leaf path, the values keep their storage class. No row
  passes through Python in the calling thread.
* What a job leaves to the calling thread is done there with the one-thread code
  (``_SqliteWriter.add``), in its place: records with more than ``writer.POOL_SAFE_BRACKETS``
  brackets (a job cuts its segment before such a record), a block whose table would need more
  columns than a table may have (``("wide",)``: the whole block), a segment that the registry
  cannot take without the overflow column or the table cap (the segment's records), a job a
  worker could not do (``("redo",)``) and every block once the workers broke. Records that cannot
  be decoded in memory are skipped (``writer.warn.deep_skipped_sqlite``), huge records too
  (``writer.warn.huge_skipped_sqlite``); a record selected twice is written once
  (``writer.warn.duplicates``: the calling thread tells a job which positions to skip).
* ``workers=1`` runs the same jobs in the calling thread (``writer._NoPool``).
* The database: ``PRAGMA page_size = 32768``, ``journal_mode = OFF``, ``synchronous = OFF``
  (inside the temporary file of ``atomic_path``: it is renamed over the target only when
  complete), one transaction committed every ``writer.SQLITE_COMMIT_BYTES`` of source; the
  indexes on every ``_parent``, the ``_jsonreshape`` and ``_wrapper`` tables at the end
  (``writer.progress.finish``), as before.
* Memory: a serialized block (a few MB) per job in flight, the registry, SQLite's page cache.
  Progress by the bytes read; ``cancel`` is checked for every block, every job result and before
  the commit. An error or a cancellation leaves no temporary file and an existing target untouched.
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
    KEY_COLUMN,
    MAX_DEPTH,
    VALUE_NAME,
    _DECODE_ERRORS,
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

PAGE_SIZE = 32768                 # PRAGMA page_size of the output database
CACHE_PAGES = -32768              # PRAGMA cache_size (KiB, negative): 32 MiB


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
    (``writer._SqliteScan.add``: objects and arrays, ``MAX_DEPTH`` levels): ``path -> [keys, widest]``."""

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
    """The row engine of :class:`writer._SqliteWriter` without a database: tables ``t0``, ``t1``, ... in
    first-seen order (no cap: the calling thread applies ``SQLITE_MAX_TABLES`` when it merges), columns by
    position (``c0``, ...; the leaf paths in ``cols``), rows kept in ``pending``; a leaf beyond the column
    limit raises :class:`_Wide` (the one-thread writer's ``_extra`` overflow is the calling thread's)."""

    def __init__(self, member: bool, check_surrogates: bool, cell_budget: int, plans: dict[tuple, _MapPlan] | None,
                 ordered: bool, max_columns: int) -> None:
        ctx = SimpleNamespace(member=member, check_surrogates=check_surrogates, cell_budget=cell_budget, cancel=None)
        super().__init__(None, ctx, plans, ordered)   # type: ignore[arg-type]
        self.max_columns = max_columns

    def _new_table(self, base: str, schema: tuple | None, parent: _SqlTable | None = None) -> _SqlTable:
        if schema is not None:
            fixed = ["_id", "_parent", "_idx"]
        else:
            fixed = ["_id"] + (["_ord"] if self.ordered else []) + ([KEY_COLUMN] if self.member else [])
        table = _SqlTable(f"t{len(self.order)}", schema, fixed, parent.name if parent is not None else None)
        self.order.append(table)
        return table

    def _table_for(self, parent: _SqlTable, rel: tuple) -> _SqlTable | None:
        schema = parent.prefix + rel
        table = self.children.get(schema)
        if table is None:
            table = self.children[schema] = self._new_table("", schema, parent)
        return table

    def _add_column(self, table: _SqlTable, text: str) -> int:
        pos = table.width
        table.width += 1
        table.colnames.append(f"c{pos}")
        return pos

    def _overflow(self, table: _SqlTable, rel: tuple, v: Any, extras: dict | None) -> dict | None:
        raise _Wide()

    def _flush(self, table: _SqlTable) -> None:
        pass


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

    def _engine(self) -> _LocalRows:
        return _LocalRows(self.member, self.check_surrogates, self.cell_budget, self.plans, self.ordered,
                          self.max_columns)

    @staticmethod
    def _segment(rows: _LocalRows, records: int, k_start: int, k_end: int, skipped: int) -> tuple:
        if not records:
            return (None, [], 0, k_start, k_end, skipped, 0, 0)
        conn = sqlite3.connect(":memory:")
        manifest = []
        try:
            for j, table in enumerate(rows.order):
                nfixed = len(table.colnames) - len(table.cols)
                leaves = [None] * len(table.cols)
                for rel, pos in table.cols.items():
                    leaves[pos - nfixed] = rel
                parent = int(table.parent[1:]) if table.parent is not None else None
                pending = table.pending
                manifest.append((table.schema, parent, leaves, len(pending)))
                if pending:
                    width = table.width
                    for r in pending:
                        if len(r) < width:
                            r.extend([None] * (width - len(r)))
                    conn.execute(f"CREATE TABLE t{j} ({','.join('c%d' % i for i in range(width))})")
                    conn.executemany(f"INSERT INTO t{j} VALUES ({','.join('?' * width)})", pending)
            blob = conn.serialize()
        finally:
            conn.close()
        return (blob, manifest, records, k_start, k_end, skipped, rows.numbers_as_text, rows.deep_values)

    def run(self, data: bytes, spans: bytes, indices: Any, skips: tuple) -> tuple:
        if type(indices) is bytes:
            indices = _pairs(indices)
        sp = _pairs(spans)
        n = len(indices)
        member, split = self.member, self.split
        stats = _KeyStats() if self.plans is None else None
        skip = frozenset(skips)
        pieces: list[Any] = []
        rows = self._engine()
        records = 0                    # records in the segment being built ...
        skipped = 0                    # ... those of its range that cannot be decoded ...
        seg_start = 0                  # ... and its first record

        def cut(k_end: int) -> None:
            nonlocal rows, records, skipped, seg_start
            if records or skipped:
                pieces.append(self._segment(rows, records, seg_start, k_end, skipped))
                if records:
                    rows = self._engine()
                records = skipped = 0
            seg_start = k_end

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
            if stats is not None:
                stats.add(value)
            try:
                rows.add(int(indices[k]), key, value, records)
            except _Wide:
                return ("wide",)
            records += 1
        cut(n)
        return ("ok", pieces, stats.summary() if stats is not None else None)


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
        conn.deserialize(blob, name="w")
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
            pool.run()
            ctx.message("writer.progress.finish")
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
