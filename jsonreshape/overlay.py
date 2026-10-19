"""Immutable edit state of a document (docs/ARCHITECTURE.md §12.0, §12.1).

The edit layer never changes the data file. It describes the *edited* document as:

* a **piece table** (:class:`PieceTable`): effective positions ``0 .. count-1`` -> stable record ids
  (*rids*; base records keep their original index, new records get ``base_count, base_count+1, ...``);
* **content layers**: ``rid -> virtual span`` mappings, newest last (:class:`DictLayer` for point edits,
  file-backed :class:`ArrayLayer` for bulk results); a rid's content is the span of the newest layer that has
  it, else (base rid) its original span in the data file;
* a **virtual address space**: offsets ``< data_size`` are the data file, offsets ``>= EDIT_BASE`` address the
  append-only segment files ``seg-<n>.dat`` of the layer (``EDIT_BASE + (seg << SEG_SHIFT) + offset``), read
  through a thread-safe :class:`DatReader`;
* :class:`OverlayState`: one immutable, identified (``gen``) snapshot of all of the above;
* :class:`Remap` / :class:`NodeChange` / :class:`ChangeSet`: how positions moved in one step.

Everything here is pure data (no Qt, no SQLite); :mod:`jsonreshape.editstore` persists it and
:mod:`jsonreshape.edits` produces it.

Decisions taken where §12.1 leaves room (all documented in the stage-3 report):

* **gen.** :func:`next_gen` is a process-wide, lock-protected ``itertools.count``. ``OverlayState`` assigns a
  fresh gen in ``__post_init__`` whenever ``gen`` is 0 (the default), so *every* constructor call yields a new
  identity; derived states are made with :meth:`OverlayState.derive` (``dataclasses.replace`` with ``gen=0``)
  or :meth:`OverlayState.with_content`. An explicit non-zero ``gen`` is accepted only when it was issued by
  :func:`next_gen` and no live state uses it (a plain ``dataclasses.replace(state, ...)``, which would copy the
  gen, therefore raises ``ValueError`` instead of creating two states with one gen). States compare by
  identity (``eq=False``) and are hashable.
* ``OverlayState.next_rid`` below ``base_count`` (the default 0) is raised to ``base_count``: new rids may
  never collide with base rids.
* **Remap "ins"/"inss" and new_rids.** :meth:`PieceTable.apply` consumes ``new_rids`` entries
  ``(pos, rid_lo, n)`` in op order; an entry assigns ``rid_lo .. rid_lo+n-1`` to the next ``n`` new items of
  the current op in ascending position order, and ``pos`` must be the position of the first of them (for
  "ins" ``op.pos + covered``, for "inss" ``positions[covered]``). One entry may therefore cover a whole
  scattered "inss"; several entries may cover one op. Missing or left-over entries raise ``ValueError``.
* **write_array_layer duplicates.** Entries with the same rid: the LAST one in input order wins (the sort is
  stable), so a producer may write a record again (e.g. a patch's LRU evicting a record twice). Memory is
  about 24 bytes per entry of one batch (<= :data:`SPILL_ENTRIES`; measured peak ~130 MB for 4 M sorted
  entries, ~150 MB for 4 M random ones) plus bounded buffers; sorted input is written without a merge.
* **ArrayLayer file.** Header = ``b"JRLAYER\\0"`` + u32 version (1) + u32 flags + u64 count + u32 crc32 +
  u32 segment count + u32 per segment, zero-padded to a multiple of 64 bytes (64 for <= 8 segments); then
  ``count`` u64 rids (strictly ascending), then ``count`` u64 ``(vstart, vend)`` pairs. The crc32 covers the
  two arrays (as §12.1 says). ``ArrayLayer.open`` checks the magic, version, size and crc32 by READING the
  file (system cache, not the process working set) before mapping it. Every damage, including a missing
  file, raises ``EditError("edits.error.layer_corrupt")``.
* **Mapping and deletion on Windows.** Layer files are opened with :func:`jsonreshape.fileio.open_shared`
  (``FILE_SHARE_DELETE``) and mapped from that handle; the mapping keeps a duplicate of the handle, so the
  file (and its whole layer folder) can be deleted while it is mapped and the mapped data stays readable
  (verified by ``tests/test_overlay_files.py``). A plain ``open()`` would block the deletion with WinError 32.
  Segment handles of :class:`DatReader` are ``open_shared`` handles too. An array layer keeps a second
  ``open_shared`` handle for its big range reads (``keys_in`` / ``spans_in`` / ``items`` of ``FILE_READ_MIN``
  values or more read the file, so a pass over millions of entries stays out of the process' working set;
  small ranges and ``get`` use the mapping); ``ArrayLayer.close`` closes both. Who closes what (§12.2): the
  ``Document`` that shows a state owns its reader and array layers and closes them after every lease ended;
  :mod:`jsonreshape.editstore` forgets segments and closes array layers whose files it deletes once no alive
  state can read them; nothing in this module closes resources on its own.
* **DatReader.publish** is monotonic (a lower end raises ``ValueError``); :meth:`DatReader.forget` drops a
  discarded segment. Any read outside the published bytes of a segment, of a span crossing segments, of a
  non-edit offset or of a missing/truncated segment raises ``EditError("edits.error.layer_corrupt")``.
* **Lifetime bookkeeping** (used by :mod:`jsonreshape.editstore` to delete layer files only when nothing can
  read them any more): every :class:`DatReader` has a process-unique :attr:`DatReader.uid` (child caches of
  edit-offset nodes are keyed with it, so two edit layers never share a cache entry), :func:`readers_of`
  lists the alive readers of a folder, :func:`live_states` the alive states, and :func:`set_reader_closed_hook`
  installs a callable that :meth:`DatReader.close` calls (without arguments; it must not block).
* **OverlayState.contents** of a rid without content: a new rid -> ``EditError(layer_corrupt)`` (the state is
  damaged); a pure base rid -> ``ValueError`` (the caller passed a pure run).
* :func:`invert_node_changes` also maps each ``NodeChange.record`` back through the inverse record remap (a
  step may change the record structure and nodes of other records at once).

Thread safety: tables, layers, remaps and states are immutable (their lazy caches are built under a lock or
are idempotent), so any thread may read them; :class:`DatReader` reads through per-thread handles;
:func:`next_gen` is locked. ``ArrayLayer.close`` / ``DatReader.close`` must not race with reads.

Performance (pure Python, ``tests/test_overlay_perf.py``): with 1,000,000 pieces an insert/delete/move of one
piece rebuilds one chunk (<= 4096 pieces) plus the chunk prefix list; ``rid_at`` is two bisects;
``rids(pos, 65536)`` is built from array slices / C-level ``range`` expansion; deleting 1,000,000 scattered
positions from a 10,000,000-record identity table is vectorized (``map``/``compress`` over arrays); an
identity table of any size is one piece. ``Remap.map`` is O(log k) per op; ``map_array`` merges an ascending
input in O(n + k) per op (falling back to C-level per-element bisects when the op is dense), general input
uses C-level bisects.
"""

from __future__ import annotations

import bisect
import dataclasses
import heapq
import itertools
import logging
import mmap
import operator
import os
import secrets
import struct
import sys
import threading
import time
import weakref
import zlib
from array import array
from dataclasses import dataclass
from itertools import accumulate, chain, compress, islice, repeat
from operator import add, and_, eq, ge, gt, itemgetter, le, lt, mul, ne, not_, or_, sub
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Mapping, Sequence, Union

from .fileio import long_path, make_dirs, normalize_path, open_shared

log = logging.getLogger(__name__)

__all__ = [
    "BLOB_MIN",
    "CHANGE_KINDS",
    "CHUNK_MAX",
    "CHUNK_MIN",
    "EDIT_BASE",
    "LAYER_MAGIC",
    "LAYER_VERSION",
    "MAX_SEGMENT",
    "RECORDS_MAX",
    "SEG_SHIFT",
    "SEG_SIZE_LIMIT",
    "SPILL_ENTRIES",
    "WINDOW",
    "ArrayLayer",
    "ChangeSet",
    "DatReader",
    "DictLayer",
    "EntryBlocks",
    "NodeChange",
    "OverlayState",
    "PieceTable",
    "Remap",
    "array_layers_of",
    "edit_offset",
    "invert_node_changes",
    "is_edit_offset",
    "live_states",
    "next_gen",
    "readers_of",
    "segment_name",
    "set_reader_closed_hook",
    "split_edit_offset",
    "write_array_layer",
]

StrPath = Union[str, "os.PathLike[str]"]

# ----------------------------------------------------------------------------------
# constants
# ----------------------------------------------------------------------------------
EDIT_BASE = 1 << 48                 # virtual offsets >= EDIT_BASE address segment files
SEG_SHIFT = 40                      # segment number field: EDIT_BASE + (seg << SEG_SHIFT) + offset
SEG_SIZE_LIMIT = 1 << SEG_SHIFT     # offsets inside a segment (span starts AND ends) stay below this
U64_MAX = (1 << 64) - 1
MAX_SEGMENT = (((1 << 64) - EDIT_BASE) >> SEG_SHIFT) - 1   # largest segment whose offsets fit in a u64

CHUNK_MAX = 4096                    # pieces per PieceTable chunk
CHUNK_MIN = 1024                    # a rebuilt region below this absorbs a neighbour chunk
WINDOW = 65536                      # OverlayState.runs(): positions per window (and max run length)
CLUSTER_GAP = 256                   # runs(): a rid gap above this starts a new override query in a window
                                    # (bounds the overrides fetched for rids that are not in the window)
BUCKET_SHIFT = 12                   # DictLayer buckets are keyed by rid >> BUCKET_SHIFT
BLOB_MIN = 4096                     # Remap.to_json(): arrays longer than this go through blob_writer
SPILL_ENTRIES = 4 * 1024 * 1024     # write_array_layer(): sorted runs are spilled beyond this many entries
SORT_BLOCK = 512 * 1024             # write_array_layer(): unsorted input is sorted in blocks of this size
MERGE_RUNS_MAX = 64                 # write_array_layer(): more natural runs than this -> block sort
MERGE_FANIN = 64                    # write_array_layer(): run files merged at once (more -> extra passes)
MERGE_BUFFER = 16 << 20             # write_array_layer(): total read buffer of a merge over run files
RECORDS_MAX = 10_000                # ChangeSet.records: more changed records -> None ("many")
CLOSE_WAIT = 1.0                    # ArrayLayer.close(): seconds to wait for a reader's slice of the mapping
FILE_READ_MIN = 4096                # ArrayLayer: range reads of this many values go through the file

LAYER_MAGIC = b"JRLAYER\0"
LAYER_VERSION = 1
MAX_LAYER_SEGMENTS = 1 << 20        # sanity bound for the segment list of an array layer header
_LAYER_HEAD = struct.Struct("<8sIIQII")   # magic, version, flags, count, crc32, nsegs (32 bytes)
_IO_CHUNK = 1 << 20
_BLOCK = 65536                      # entries per block when streaming entries / runs

CHANGE_KINDS = frozenset({"edit", "undo", "redo", "bulk", "patch", "restore", "discard"})

_BIG_ENDIAN = sys.byteorder == "big"
_EMPTY = array("Q")


# ----------------------------------------------------------------------------------
# errors
# ----------------------------------------------------------------------------------
def _layer_corrupt(path: object = "", detail: str = "") -> Exception:
    """``EditError("edits.error.layer_corrupt")`` (the edit layer on disk is damaged). ``path`` and
    ``detail`` are kept as error params (and logged) for diagnosis; the message itself names neither."""
    from jsonreshape.errors import EditError  # lazy: errors.py is extended by the editing stage

    log.warning("edit layer damaged: %s (%s)", path or "-", detail or "-")
    return EditError("edits.error.layer_corrupt", path=str(path), detail=detail)


# ----------------------------------------------------------------------------------
# virtual address space
# ----------------------------------------------------------------------------------
def is_edit_offset(offset: int) -> bool:
    """True for an offset in the segment address space (``offset >= EDIT_BASE``).

    Not to be confused with ``Node.is_virtual`` (the synthetic jsonl/stream root)."""
    return offset >= EDIT_BASE


def edit_offset(seg: int, off: int) -> int:
    """Virtual offset of byte ``off`` of segment ``seg``."""
    seg = operator.index(seg)
    off = operator.index(off)
    if not 0 <= seg <= MAX_SEGMENT:
        raise ValueError(f"segment number out of range: {seg}")
    if not 0 <= off < SEG_SIZE_LIMIT:
        raise ValueError(f"segment offset out of range: {off}")
    return EDIT_BASE + (seg << SEG_SHIFT) + off


def split_edit_offset(v: int) -> tuple[int, int]:
    """``(seg, off)`` of a virtual offset (``ValueError`` for an offset below ``EDIT_BASE``)."""
    v = operator.index(v)
    if v < EDIT_BASE or v > U64_MAX:
        raise ValueError(f"not an edit offset: {v}")
    rel = v - EDIT_BASE
    return rel >> SEG_SHIFT, rel & (SEG_SIZE_LIMIT - 1)


def segment_name(seg: int) -> str:
    """File name of segment ``seg`` inside a layer directory (``seg-00000.dat``)."""
    return f"seg-{operator.index(seg):05d}.dat"


# ----------------------------------------------------------------------------------
# small array helpers
# ----------------------------------------------------------------------------------
def _nonneg(value: Any, what: str = "value") -> int:
    v = operator.index(value)
    if v < 0:
        raise ValueError(f"{what} must not be negative: {v}")
    return v


def _u64(values: Any, what: str = "values") -> array:
    """``values`` as ``array('Q')`` (the same object when it already is one)."""
    if isinstance(values, array) and values.typecode == "Q":
        return values
    try:
        return array("Q", values)
    except (TypeError, OverflowError, ValueError) as exc:
        raise ValueError(f"{what}: expected unsigned 64-bit integers") from exc


def _copy_u64(values: Any, what: str = "values") -> array:
    """A new ``array('Q')`` holding ``values``."""
    try:
        return array("Q", values)
    except (TypeError, OverflowError, ValueError) as exc:
        raise ValueError(f"{what}: expected unsigned 64-bit integers") from exc


def _strictly_ascending(a: Sequence[int]) -> bool:
    return len(a) < 2 or all(map(lt, a, islice(a, 1, None)))


def _nondecreasing(a: Sequence[int]) -> bool:
    return len(a) < 2 or all(map(le, a, islice(a, 1, None)))


def _positions(values: Any, what: str = "positions") -> array:
    """A copy of ``values`` as ``array('Q')``, checked to be strictly ascending."""
    a = _copy_u64(values, what)
    if not _strictly_ascending(a):
        raise ValueError(f"{what} must be ascending and unique")
    return a


def _le_bytes(a: array) -> bytes:
    if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only
        a = array("Q", a)
        a.byteswap()
    return a.tobytes()


def _from_le(data: bytes | bytearray | memoryview, what: str = "data") -> array:
    if len(data) % 8:
        raise ValueError(f"{what}: length {len(data)} is not a multiple of 8")
    a = array("Q")
    a.frombytes(data)
    if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only
        a.byteswap()
    return a


def _interleave(a: array, b: array) -> array:
    """``a0, b0, a1, b1, ...`` (C-level strided slice assignment)."""
    out = array("Q", bytes(16 * len(a)))
    out[0::2] = a
    out[1::2] = b
    return out


def _consecutive_pieces(rids: array) -> tuple[array, array]:
    """Rid sequence -> maximal ranges of consecutive rids ``(lo, hi)``."""
    n = len(rids)
    if not n:
        return array("Q"), array("Q")
    if rids[-1] - rids[0] + 1 == n and _strictly_ascending(rids):
        return array("Q", [rids[0]]), array("Q", [rids[-1] + 1])
    breaks = list(compress(range(1, n), map(ne, islice(rids, 1, None), map(add, rids, repeat(1)))))
    starts = [0, *breaks]
    lo = array("Q", map(rids.__getitem__, starts))
    hi = array("Q", map(add, map(rids.__getitem__, map(sub, [*breaks, n], repeat(1))), repeat(1)))
    return lo, hi


# ----------------------------------------------------------------------------------
# PieceTable
# ----------------------------------------------------------------------------------
class _Chunk:
    """At most CHUNK_MAX pieces: rid ranges ``[lo[i], hi[i])`` and the local position prefix ``pre``
    (``pre[i]`` = position of piece i inside the chunk, ``pre[-1]`` = records in the chunk). Immutable."""

    __slots__ = ("count", "hi", "lo", "pre")

    def __init__(self, lo: array, hi: array) -> None:
        self.lo = lo
        self.hi = hi
        self.pre = array("Q", accumulate(map(sub, hi, lo), initial=0))
        self.count = self.pre[-1]


def _normalize(lo: array, hi: array) -> tuple[array, array]:
    """Drop empty pieces and merge contiguous neighbours. The inputs must be arrays the caller owns
    (they may be returned unchanged)."""
    m = len(lo)
    if len(hi) != m:
        raise ValueError("lo and hi must have the same length")
    if not m:
        return lo, hi
    if not all(map(lt, lo, hi)):
        if any(map(gt, lo, hi)):
            raise ValueError("invalid piece: lo > hi")
        keep = bytes(map(lt, lo, hi))
        lo = array("Q", compress(lo, keep))
        hi = array("Q", compress(hi, keep))
        m = len(lo)
        if not m:
            return lo, hi
    joins = list(compress(range(1, m), map(eq, hi, islice(lo, 1, None))))
    if not joins:
        return lo, hi
    first = bytearray(b"\x01") * m
    for j in joins:
        first[j] = 0
    last = first[1:] + b"\x01"
    return array("Q", compress(lo, first)), array("Q", compress(hi, last))


def _make_chunks(lo: array, hi: array) -> list[_Chunk]:
    """Normalized flat pieces -> chunks of near-equal size (<= CHUNK_MAX). Takes ownership of the arrays."""
    m = len(lo)
    if not m:
        return []
    k = -(-m // CHUNK_MAX)
    if k == 1:
        return [_Chunk(lo, hi)]
    return [_Chunk(lo[m * j // k:m * (j + 1) // k], hi[m * j // k:m * (j + 1) // k]) for j in range(k)]


def _assemble_chunks(parts: Sequence[_Chunk | tuple[array, array]]) -> list[_Chunk]:
    """Concatenate shared chunks and owned flat ``(lo, hi)`` piece lists into canonical chunks.

    Junctions whose pieces are contiguous are merged (canonical form: no two adjacent pieces with
    ``hi == next lo``, also across chunks); flats are re-chunked; a flat smaller than CHUNK_MIN absorbs a
    neighbour so that every chunk but a lone one keeps >= CHUNK_MIN pieces. Untouched chunks are shared."""
    items: list[list[Any]] = []   # [lo, hi, chunk | None]  (chunk None = owned flat)
    for part in parts:
        if isinstance(part, _Chunk):
            if not part.lo:
                continue
            lo, hi, ch = part.lo, part.hi, part
        else:
            lo, hi = _normalize(part[0], part[1])
            if not lo:
                continue
            ch = None
        if items:
            last = items[-1]
            joined = last[1][-1] == lo[0]
            if joined or (last[2] is None and ch is None):
                if last[2] is not None:  # a shared chunk becomes an owned flat
                    last[0], last[1], last[2] = array("Q", last[0]), array("Q", last[1]), None
                if joined:
                    last[1][-1] = hi[0]
                    last[0].extend(lo[1:])
                    last[1].extend(hi[1:])
                else:
                    last[0].extend(lo)
                    last[1].extend(hi)
                continue
        items.append([lo, hi, ch])
    # balance: small flats absorb a neighbour (junctions are known not to be contiguous here)
    i = 0
    while i < len(items):
        it = items[i]
        if it[2] is None and len(it[0]) < CHUNK_MIN and len(items) > 1:
            left = items[i - 1] if i > 0 else None
            right = items[i + 1] if i + 1 < len(items) else None
            if right is not None and (left is None or len(right[0]) <= len(left[0])):
                lo, hi = it[0], it[1]
                lo.extend(right[0])
                hi.extend(right[1])
                del items[i + 1]
                # a flat after the absorbed chunk is now adjacent: concatenate it too
                if i + 1 < len(items) and items[i + 1][2] is None:
                    lo.extend(items[i + 1][0])
                    hi.extend(items[i + 1][1])
                    del items[i + 1]
            else:
                assert left is not None
                lo = array("Q", left[0])
                hi = array("Q", left[1])
                lo.extend(it[0])
                hi.extend(it[1])
                items[i - 1] = [lo, hi, None]
                del items[i]
                i -= 1
                if i > 0 and items[i - 1][2] is None:
                    prev = items[i - 1]
                    prev[0].extend(items[i][0])
                    prev[1].extend(items[i][1])
                    del items[i]
                    i -= 1
            continue
        i += 1
    out: list[_Chunk] = []
    for lo, hi, ch in items:
        if ch is not None:
            out.append(ch)
        else:
            out.extend(_make_chunks(lo, hi))
    return out


def _cut(ch: _Chunk, local: int) -> tuple[array, array, array, array]:
    """Owned copies of the pieces of ``ch`` before / from local position ``local``."""
    if local <= 0:
        return array("Q"), array("Q"), array("Q", ch.lo), array("Q", ch.hi)
    if local >= ch.count:
        return array("Q", ch.lo), array("Q", ch.hi), array("Q"), array("Q")
    i = bisect.bisect_right(ch.pre, local) - 1
    off = local - ch.pre[i]
    if not off:
        return ch.lo[:i], ch.hi[:i], ch.lo[i:], ch.hi[i:]
    mid = ch.lo[i] + off
    llo, lhi = ch.lo[:i + 1], ch.hi[:i + 1]
    lhi[i] = mid
    rlo, rhi = ch.lo[i:], ch.hi[i:]
    rlo[0] = mid
    return llo, lhi, rlo, rhi


def _gaps(base: int, s: int, e: int, dl: array) -> tuple[array, array]:
    """Piece covering local positions ``[s, e)`` with rids from ``base``, minus the ascending positions
    ``dl`` (all inside the piece) -> the kept sub-pieces (vectorized)."""
    st = array("Q", [s])
    st.extend(map(add, dl, repeat(1)))
    en = array("Q", dl)
    en.append(e)
    keep = bytes(map(lt, st, en))
    delta = base - s
    if delta:
        return (array("Q", compress(map(add, st, repeat(delta)), keep)),
                array("Q", compress(map(add, en, repeat(delta)), keep)))
    return array("Q", compress(st, keep)), array("Q", compress(en, keep))


def _delete_local(ch: _Chunk, dl: array) -> tuple[array, array]:
    """Pieces of ``ch`` without the ascending local positions ``dl``."""
    pre, lo, hi = ch.pre, ch.lo, ch.hi
    if len(lo) == 1:
        return _gaps(lo[0], 0, ch.count, dl)
    pidx = array("Q", map(bisect.bisect_right, repeat(pre), dl))   # piece index + 1
    out_lo, out_hi = array("Q"), array("Q")
    k = len(dl)
    g = 0
    nxt = 0
    while g < k:
        q = pidx[g]
        g2 = bisect.bisect_right(pidx, q, g)
        i = q - 1
        if i > nxt:
            out_lo.extend(lo[nxt:i])
            out_hi.extend(hi[nxt:i])
        s, e, b = pre[i], pre[i + 1], lo[i]
        if g2 - g == 1:
            d = dl[g]
            if d > s:
                out_lo.append(b)
                out_hi.append(b + d - s)
            if d + 1 < e:
                out_lo.append(b + d + 1 - s)
                out_hi.append(hi[i])
        else:
            glo, ghi = _gaps(b, s, e, dl[g:g2])
            out_lo.extend(glo)
            out_hi.extend(ghi)
        nxt = i + 1
        g = g2
    out_lo.extend(lo[nxt:])
    out_hi.extend(hi[nxt:])
    return out_lo, out_hi


def _emit(ch: _Chunk, i0: int, off0: int, i1: int, off1: int, out_lo: array, out_hi: array) -> None:
    """Append the records of ``ch`` from cursor (piece i0, offset off0) to cursor (i1, off1), exclusive."""
    lo, hi = ch.lo, ch.hi
    if i0 == i1:
        if off1 > off0:
            out_lo.append(lo[i0] + off0)
            out_hi.append(lo[i0] + off1)
        return
    out_lo.append(lo[i0] + off0)
    out_hi.append(hi[i0])
    if i1 > i0 + 1:
        out_lo.extend(lo[i0 + 1:i1])
        out_hi.extend(hi[i0 + 1:i1])
    if off1:
        out_lo.append(lo[i1])
        out_hi.append(lo[i1] + off1)


_INDEX_LOCK = threading.Lock()


class PieceTable:
    """Immutable, persistent map ``position -> rid`` (§12.1).

    The effective order is a list of *pieces*, rid ranges ``[lo, hi)``, stored in chunks of at most
    :data:`CHUNK_MAX` pieces (``array('Q')`` lo/hi + a per-chunk position prefix) with a chunk prefix list.
    Every operation returns a new table that shares the untouched chunks (a point edit rebuilds one chunk:
    ``O(P/4096 + 4096)``). Adjacent contiguous pieces are always merged (canonical form), so two tables are
    equal exactly when they map every position to the same rid. An identity table of any size is one piece.

    Positions and rids are Python ints in ``0 .. 2**64-1``. Rids must be unique within a table; operations
    trust their caller to insert fresh rids (``from_pairs`` / ``from_bytes`` validate).
    """

    __slots__ = ("__weakref__", "_chunks", "_count", "_index", "_npieces", "_starts")

    def __init__(self, chunks: Iterable[_Chunk] = ()) -> None:
        """Internal constructor (use :meth:`identity`, :meth:`from_pairs`, :meth:`from_bytes`)."""
        self._chunks: tuple[_Chunk, ...] = tuple(chunks)
        self._starts: list[int] = list(accumulate((c.count for c in self._chunks), initial=0))
        self._count: int = self._starts[-1]
        self._npieces: int = sum(len(c.lo) for c in self._chunks)
        self._index: tuple[array, array, array] | None = None

    # -- construction ------------------------------------------------------------------
    @classmethod
    def identity(cls, n: int) -> PieceTable:
        """Positions ``0 .. n-1`` -> rids ``0 .. n-1`` (one piece; O(1) memory)."""
        n = _nonneg(n, "n")
        if n > U64_MAX:
            raise ValueError("n too large")
        if not n:
            return cls(())
        return cls((_Chunk(array("Q", [0]), array("Q", [n])),))

    @classmethod
    def from_pairs(cls, lo: Any, hi: Any) -> PieceTable:
        """Table of the pieces ``[lo[i], hi[i])`` in this order (empty pieces dropped, contiguous ones merged).
        ``ValueError`` when ``lo > hi`` or two pieces share a rid."""
        lo_a, hi_a = _copy_u64(lo, "lo"), _copy_u64(hi, "hi")
        lo_a, hi_a = _normalize(lo_a, hi_a)
        _check_disjoint(lo_a, hi_a)
        return cls(_make_chunks(lo_a, hi_a))

    @classmethod
    def from_bytes(cls, b: bytes | bytearray | memoryview) -> PieceTable:
        """Inverse of :meth:`to_bytes` (u64 LE ``lo, hi`` pairs); validates every piece."""
        if len(b) % 16:
            raise ValueError("piece table blob: length is not a multiple of 16")
        pairs = _from_le(b, "piece table blob")
        lo, hi = pairs[0::2], pairs[1::2]
        if not all(map(lt, lo, hi)):
            raise ValueError("piece table blob: empty or reversed piece")
        lo, hi = _normalize(lo, hi)
        _check_disjoint(lo, hi)
        return cls(_make_chunks(lo, hi))

    def to_bytes(self) -> bytes:
        """u64 LE ``(lo, hi)`` pairs of the canonical pieces."""
        lo, hi = self._flat()
        return _le_bytes(_interleave(lo, hi))

    # -- basic properties ---------------------------------------------------------------
    @property
    def count(self) -> int:
        """Number of positions (records)."""
        return self._count

    @property
    def npieces(self) -> int:
        """Number of canonical pieces."""
        return self._npieces

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PieceTable):
            return NotImplemented
        if self is other:
            return True
        if self._count != other._count or self._npieces != other._npieces:
            return False
        a_lo, a_hi = self._flat()
        b_lo, b_hi = other._flat()
        return a_lo == b_lo and a_hi == b_hi

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"PieceTable(count={self._count}, npieces={self._npieces}, chunks={len(self._chunks)})"

    def _flat(self) -> tuple[array, array]:
        """All pieces as (lo, hi) arrays. Read-only: for a single chunk these are the chunk's arrays."""
        chunks = self._chunks
        if len(chunks) == 1:
            return chunks[0].lo, chunks[0].hi
        lo, hi = array("Q"), array("Q")
        for ch in chunks:
            lo.extend(ch.lo)
            hi.extend(ch.hi)
        return lo, hi

    # -- reading -----------------------------------------------------------------------
    def rid_at(self, pos: int) -> int:
        """Rid at ``pos`` (``IndexError`` outside ``0 .. count-1``)."""
        pos = operator.index(pos)
        if pos < 0 or pos >= self._count:
            raise IndexError(f"position {pos} out of range (count {self._count})")
        chunks = self._chunks
        if len(chunks) == 1:
            ch = chunks[0]
            if len(ch.lo) == 1:
                return ch.lo[0] + pos
        else:
            c = bisect.bisect_right(self._starts, pos) - 1
            ch = chunks[c]
            pos -= self._starts[c]
        i = bisect.bisect_right(ch.pre, pos) - 1
        return ch.lo[i] + pos - ch.pre[i]

    def _segments(self, pos0: int, pos1: int) -> tuple[array, array, array]:
        """Piece segments covering positions ``[pos0, pos1)`` (clipped): (position starts, rid starts,
        lengths) as fresh ``array('Q')``s."""
        ps, ls, ns = array("Q"), array("Q"), array("Q")
        if pos0 < 0:
            pos0 = 0
        if pos1 > self._count:
            pos1 = self._count
        if pos1 <= pos0:
            return ps, ls, ns
        starts, chunks = self._starts, self._chunks
        nc = len(chunks)
        c = bisect.bisect_right(starts, pos0) - 1
        while c < nc:
            cs = starts[c]
            if cs >= pos1:
                break
            ch = chunks[c]
            a = pos0 - cs if pos0 > cs else 0
            b = pos1 - cs
            if b > ch.count:
                b = ch.count
            pre = ch.pre
            i0 = bisect.bisect_right(pre, a) - 1
            i1 = bisect.bisect_left(pre, b, i0 + 1)
            lo = ch.lo[i0:i1]
            lens = array("Q", map(sub, ch.hi[i0:i1], lo))
            pst = pre[i0:i1]
            d = a - pst[0]
            if d:
                lo[0] += d
                lens[0] -= d
                pst[0] = a
            e = pre[i1] - b
            if e:
                lens[-1] -= e
            if cs:
                pst = array("Q", map(add, pst, repeat(cs)))
            ps.extend(pst)
            ls.extend(lo)
            ns.extend(lens)
            c += 1
        return ps, ls, ns

    def rids(self, pos: int, n: int) -> array:
        """Rids of positions ``pos .. pos+n-1`` as ``array('Q')`` (the window clipped to the table)."""
        pos = operator.index(pos)
        end = min(self._count, pos + operator.index(n))
        pos = max(0, pos)
        if end <= pos:
            return array("Q")
        _ps, ls, ns = self._segments(pos, end)
        if len(ls) == 1:
            return array("Q", range(ls[0], ls[0] + ns[0]))
        if len(ls) == end - pos:   # unit pieces: the rid starts are the answer
            return ls
        return array("Q", chain.from_iterable(map(range, ls, map(add, ls, ns))))

    def rids_at(self, positions: Any) -> array:
        """Rids of arbitrary positions (any order, duplicates allowed); identity fast path for one piece."""
        pos = _u64(positions, "positions")
        if not pos:
            return array("Q")
        if max(pos) >= self._count:
            raise IndexError(f"position {max(pos)} out of range (count {self._count})")
        if self._npieces == 1:
            lo0 = self._chunks[0].lo[0]
            return array("Q", pos) if not lo0 else array("Q", map(add, pos, repeat(lo0)))
        if _nondecreasing(pos):
            return self._rids_sorted(pos)
        return array("Q", map(self.rid_at, pos))

    def _rids_sorted(self, pos: array) -> array:
        out = array("Q")
        starts, chunks = self._starts, self._chunks
        k = len(pos)
        i = 0
        while i < k:
            p = pos[i]
            c = bisect.bisect_right(starts, p) - 1
            ch, cs = chunks[c], starts[c]
            pi = bisect.bisect_right(ch.pre, p - cs) - 1
            j = bisect.bisect_left(pos, ch.pre[pi + 1] + cs, i)
            delta = ch.lo[pi] - ch.pre[pi] - cs
            seg = pos[i:j]
            out.extend(map(add, seg, repeat(delta)) if delta else seg)
            i = j
        return out

    def _rid_index(self) -> tuple[array, array, array]:
        """Rid-sorted (lo, hi, position) arrays with a dummy piece (0, 0, 0) at index 0; built once."""
        idx = self._index
        if idx is not None:
            return idx
        with _INDEX_LOCK:
            idx = self._index
            if idx is None:
                lo, hi = self._flat()
                pos = array("Q", accumulate(map(sub, hi, lo), initial=0))
                pos.pop()
                if not _strictly_ascending(lo):
                    order = sorted(range(len(lo)), key=lo.__getitem__)
                    lo = array("Q", map(lo.__getitem__, order))
                    hi = array("Q", map(hi.__getitem__, order))
                    pos = array("Q", map(pos.__getitem__, order))
                if not all(map(le, hi, islice(lo, 1, None))):
                    raise ValueError("piece table holds a rid more than once")
                ilo, ihi, ipos = array("Q", [0]), array("Q", [0]), array("Q", [0])
                ilo.extend(lo)
                ihi.extend(hi)
                ipos.extend(pos)
                idx = (ilo, ihi, ipos)
                self._index = idx
        return idx

    def pos_of(self, rid: int) -> int | None:
        """Position of ``rid`` or ``None`` (lazily built rid-sorted index, cached per table, thread-safe)."""
        rid = operator.index(rid)
        if self._npieces == 1:
            ch = self._chunks[0]
            return rid - ch.lo[0] if ch.lo[0] <= rid < ch.hi[0] else None
        if self._npieces == 0 or rid < 0:
            return None
        ilo, ihi, ipos = self._rid_index()
        i = bisect.bisect_right(ilo, rid) - 1
        if rid >= ihi[i]:
            return None
        return ipos[i] + rid - ilo[i]

    def _pos_of_array(self, rids: array) -> tuple[array, bytes]:
        """Vectorized :meth:`pos_of`: (positions, keep mask); positions of missing rids are meaningless."""
        if not rids:
            return array("Q"), b""
        if self._npieces == 0:
            return array("Q", bytes(8 * len(rids))), bytes(len(rids))
        ilo, ihi, ipos = self._rid_index()
        ix = array("Q", map(sub, map(bisect.bisect_right, repeat(ilo), rids), repeat(1)))
        keep = bytes(map(lt, rids, map(ihi.__getitem__, ix)))
        pos = array("Q", map(add, map(ipos.__getitem__, ix), map(sub, rids, map(ilo.__getitem__, ix))))
        return pos, keep

    def runs(self, pos0: int = 0, pos1: int | None = None) -> Iterator[tuple[int, int, int]]:
        """``(pos, rid_lo, n)`` for every piece overlapping ``[pos0, pos1)`` (clipped), in position order."""
        pos0 = max(0, operator.index(pos0))
        pos1 = self._count if pos1 is None else min(self._count, operator.index(pos1))
        starts = self._starts
        nc = len(self._chunks)
        c = bisect.bisect_right(starts, pos0) - 1
        while 0 <= c < nc and starts[c] < pos1:
            ps, ls, ns = self._segments(max(pos0, starts[c]), min(pos1, starts[c + 1]))
            yield from zip(ps, ls, ns)
            c += 1

    def _slice_pieces(self, pos0: int, pos1: int) -> tuple[array, array]:
        """Owned (lo, hi) pieces of positions ``[pos0, pos1)``."""
        _ps, ls, ns = self._segments(pos0, pos1)
        return ls, array("Q", map(add, ls, ns))

    # -- structural operations ---------------------------------------------------------
    def _splice(self, pos0: int, pos1: int, lo: array, hi: array) -> PieceTable:
        """Replace positions ``[pos0, pos1)`` by the owned pieces ``(lo, hi)``."""
        chunks = self._chunks
        if not chunks:
            return PieceTable(_assemble_chunks([(lo, hi)]))
        starts = self._starts
        nc = len(chunks)
        a = min(bisect.bisect_right(starts, pos0) - 1, nc - 1)
        b = a if pos1 <= pos0 else min(bisect.bisect_right(starts, pos1 - 1) - 1, nc - 1)
        flo, fhi, _x, _y = _cut(chunks[a], pos0 - starts[a])
        _x, _y, rlo, rhi = _cut(chunks[b], pos1 - starts[b])
        flo.extend(lo)
        fhi.extend(hi)
        flo.extend(rlo)
        fhi.extend(rhi)
        parts: list[Any] = []
        first = a - 1 if a > 0 else a
        if a > 0:
            parts.append(chunks[a - 1])
        parts.append((flo, fhi))
        if b + 1 < nc:
            parts.append(chunks[b + 1])
        mid = _assemble_chunks(parts)
        return PieceTable(chunks[:first] + tuple(mid) + chunks[b + 2:])

    def insert(self, pos: int, rid_lo: int, n: int) -> PieceTable:
        """Insert the rids ``rid_lo .. rid_lo+n-1`` at ``pos`` (``0 <= pos <= count``)."""
        pos, rid_lo, n = operator.index(pos), _nonneg(rid_lo, "rid_lo"), _nonneg(n, "n")
        if not 0 <= pos <= self._count:
            raise IndexError(f"insert position {pos} out of range (count {self._count})")
        if rid_lo + n > U64_MAX:
            raise ValueError("rid range too large")
        if not n:
            return self
        return self._splice(pos, pos, array("Q", [rid_lo]), array("Q", [rid_lo + n]))

    def _insert_pieces(self, pos: int, lo: array, hi: array) -> PieceTable:
        if not 0 <= pos <= self._count:
            raise IndexError(f"insert position {pos} out of range (count {self._count})")
        if not lo:
            return self
        return self._splice(pos, pos, lo, hi)

    def delete(self, pos: int, n: int) -> PieceTable:
        """Remove positions ``pos .. pos+n-1``."""
        pos, n = operator.index(pos), _nonneg(n, "n")
        if pos < 0 or pos + n > self._count:
            raise IndexError(f"delete range {pos}+{n} out of range (count {self._count})")
        if not n:
            return self
        return self._splice(pos, pos + n, array("Q"), array("Q"))

    def delete_positions(self, positions: Any) -> PieceTable:
        """Remove the records at ``positions`` (ascending, unique)."""
        d = _u64(positions, "positions")
        k = len(d)
        if not k:
            return self
        if not _strictly_ascending(d):
            raise ValueError("positions must be ascending and unique")
        if d[-1] >= self._count:
            raise IndexError(f"position {d[-1]} out of range (count {self._count})")
        if d[-1] - d[0] + 1 == k:
            return self.delete(d[0], k)
        return self._delete_scattered(d)

    def _delete_scattered(self, d: array) -> PieceTable:
        chunks, starts = self._chunks, self._starts
        parts: list[Any] = []
        k = len(d)
        j = 0
        c_next = 0
        while j < k:
            c = bisect.bisect_right(starts, d[j]) - 1
            parts.extend(chunks[c_next:c])
            cs, ce = starts[c], starts[c + 1]
            j2 = bisect.bisect_left(d, ce, j)
            local = array("Q", map(sub, d[j:j2], repeat(cs))) if cs else d[j:j2]
            parts.append(_delete_local(chunks[c], local))
            c_next = c + 1
            j = j2
        parts.extend(chunks[c_next:])
        return PieceTable(_assemble_chunks(parts))

    def move(self, positions: Any, to: int) -> PieceTable:
        """Take the records at ``positions`` (ascending, unique) out and re-insert them in their relative
        order so that the first one ends at ``to``, where ``to`` counts positions of the table WITHOUT the
        moved records (``0 <= to <= count - len(positions)``)."""
        p = _u64(positions, "positions")
        to = operator.index(to)
        k = len(p)
        if not k:
            if not 0 <= to <= self._count:
                raise IndexError(f"move target {to} out of range (count {self._count})")
            return self
        if not _strictly_ascending(p):
            raise ValueError("positions must be ascending and unique")
        if p[-1] >= self._count:
            raise IndexError(f"position {p[-1]} out of range (count {self._count})")
        if not 0 <= to <= self._count - k:
            raise IndexError(f"move target {to} out of range (count {self._count}, moving {k})")
        p0 = p[0]
        if p[-1] - p0 + 1 == k:
            if to == p0:
                return self
            mlo, mhi = self._slice_pieces(p0, p0 + k)
            rest = self._splice(p0, p0 + k, array("Q"), array("Q"))
        else:
            mlo, mhi = _consecutive_pieces(self.rids_at(p))
            rest = self._delete_scattered(p)
        return rest._splice(to, to, mlo, mhi)

    def _unmove(self, to: int, positions: Any) -> PieceTable:
        """Inverse of :meth:`move`: the records at ``[to, to+k)`` go back to ``positions`` (final coords)."""
        p = _u64(positions, "positions")
        to = operator.index(to)
        k = len(p)
        if not k:
            return self
        if not _strictly_ascending(p):
            raise ValueError("positions must be ascending and unique")
        if p[-1] >= self._count or not 0 <= to <= self._count - k:
            raise IndexError(f"unmove out of range (count {self._count}, to {to}, moving {k})")
        mlo, mhi = self._slice_pieces(to, to + k)
        rest = self._splice(to, to + k, array("Q"), array("Q"))
        return rest._insert_at(p, mlo, mhi)

    def _insert_at(self, positions: array, lo: array, hi: array) -> PieceTable:
        """New items whose rids are the pieces ``(lo, hi)`` (in order) land at the ascending FINAL
        ``positions``."""
        p = positions
        k = len(p)
        if not k:
            if lo and any(map(lt, lo, hi)):
                raise ValueError("new rids without positions")
            return self
        if not _strictly_ascending(p):
            raise ValueError("positions must be ascending and unique")
        if p[-1] >= self._count + k:
            raise IndexError(f"insert position {p[-1]} out of range (count after {self._count + k})")
        total = sum(map(sub, hi, lo))
        if total != k:
            raise ValueError(f"{k} positions but {total} new rids")
        if p[-1] - p[0] + 1 == k:
            return self._splice(p[0], p[0], lo, hi)
        rids = array("Q", chain.from_iterable(map(range, lo, hi)))
        gaps = array("Q", map(sub, p, range(k)))     # old position each new item goes before
        breaks = list(compress(range(1, k), map(ne, islice(gaps, 1, None), gaps)))
        bounds = [0, *breaks, k]
        events = []
        for a, b in zip(bounds, islice(bounds, 1, None)):
            elo, ehi = _consecutive_pieces(rids[a:b])
            events.append((gaps[a], elo, ehi))
        return self._insert_events(events)

    def _insert_events(self, events: list[tuple[int, array, array]]) -> PieceTable:
        """Insert piece lists at strictly increasing OLD positions (``0 <= g <= count``)."""
        chunks, starts = self._chunks, self._starts
        # an empty table only receives contiguous inserts (handled by _insert_at through _splice)
        assert chunks, "_insert_events on an empty table"
        nc = len(chunks)
        parts: list[Any] = []
        c_next = 0
        e = 0
        n_ev = len(events)
        while e < n_ev:
            g = events[e][0]
            c = min(bisect.bisect_right(starts, g) - 1, nc - 1)
            parts.extend(chunks[c_next:c])
            ch, cs = chunks[c], starts[c]
            limit = starts[c + 1] if c < nc - 1 else starts[c + 1] + 1
            out_lo, out_hi = array("Q"), array("Q")
            ci, co = 0, 0
            npc = len(ch.lo)
            while e < n_ev and events[e][0] < limit:
                g, elo, ehi = events[e]
                local = g - cs
                if local >= ch.count:
                    i, off = npc, 0
                else:
                    i = bisect.bisect_right(ch.pre, local) - 1
                    off = local - ch.pre[i]
                _emit(ch, ci, co, i, off, out_lo, out_hi)
                out_lo.extend(elo)
                out_hi.extend(ehi)
                ci, co = i, off
                e += 1
            if ci < npc:
                _emit(ch, ci, co, npc, 0, out_lo, out_hi)
            parts.append((out_lo, out_hi))
            c_next = c + 1
        parts.extend(chunks[c_next:])
        return PieceTable(_assemble_chunks(parts))

    def apply(self, remap: Remap, *, new_rids: Sequence[tuple[int, int, int]] = ()) -> PieceTable:
        """Replay the structural ops of ``remap``; "ins"/"inss" take their rids from ``new_rids``
        ``(pos, rid_lo, n)`` in op order (see the module docstring); "pieces" requires this table to equal
        the op's old table and yields its new one."""
        feed = _RidFeed(new_rids)
        t = self
        for op in remap.ops:
            kind = op[0]
            if kind == "ins":
                lo, hi = feed.take(op[2], start=op[1])
                t = t._insert_pieces(op[1], lo, hi)
            elif kind == "del":
                t = t.delete(op[1], op[2])
            elif kind == "inss":
                lo, hi = feed.take(len(op[1]), positions=op[1])
                t = t._insert_at(op[1], lo, hi)
            elif kind == "dels":
                t = t.delete_positions(op[1])
            elif kind == "move":
                t = t.move(op[1], op[2])
            elif kind == "unmove":
                t = t._unmove(op[1], op[2])
            else:  # pieces
                if t != op[1]:
                    raise ValueError("'pieces' remap op does not start from this piece table")
                t = op[2]
        feed.finish()
        return t


def _check_disjoint(lo: array, hi: array) -> None:
    """``ValueError`` when two pieces share a rid."""
    if len(lo) < 2:
        return
    if _strictly_ascending(lo):
        slo, shi = lo, hi
    else:
        order = sorted(range(len(lo)), key=lo.__getitem__)
        slo = array("Q", map(lo.__getitem__, order))
        shi = array("Q", map(hi.__getitem__, order))
    if not all(map(le, shi, islice(slo, 1, None))):
        raise ValueError("a rid occurs in more than one piece")


class _RidFeed:
    """Consumes ``new_rids`` entries for PieceTable.apply (see the module docstring)."""

    def __init__(self, entries: Sequence[tuple[int, int, int]]) -> None:
        self._entries: list[tuple[int, int, int]] = []
        for entry in entries:
            if len(entry) != 3:
                raise ValueError(f"new_rids entry must be (pos, rid_lo, n): {entry!r}")
            pos, rid_lo, n = (_nonneg(v, "new_rids") for v in entry)
            if not n or rid_lo + n > U64_MAX:
                raise ValueError(f"invalid new_rids entry: {entry!r}")
            self._entries.append((pos, rid_lo, n))
        self._i = 0

    def take(self, k: int, *, start: int | None = None, positions: array | None = None) -> tuple[array, array]:
        lo, hi = array("Q"), array("Q")
        covered = 0
        entries = self._entries
        while covered < k:
            if self._i >= len(entries):
                raise ValueError("new_rids: not enough entries for the inserted items")
            pos, rid_lo, n = entries[self._i]
            self._i += 1
            want = start + covered if positions is None else positions[covered]  # type: ignore[operator]
            if pos != want:
                raise ValueError(f"new_rids entry starts at position {pos}, expected {want}")
            if covered + n > k:
                raise ValueError("new_rids entry covers more items than the op inserts")
            lo.append(rid_lo)
            hi.append(rid_lo + n)
            covered += n
        return lo, hi

    def finish(self) -> None:
        if self._i != len(self._entries):
            raise ValueError("new_rids: more entries than inserted items")


# ----------------------------------------------------------------------------------
# content layers
# ----------------------------------------------------------------------------------
def _span(value: Any) -> tuple[int, int]:
    try:
        s, e = value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"span must be (start, end): {value!r}") from exc
    s, e = operator.index(s), operator.index(e)
    if not 0 <= s <= e <= U64_MAX:
        raise ValueError(f"invalid span: ({s}, {e})")
    return s, e


class _Bucket(dict):
    """One DictLayer bucket (rids with the same ``rid >> BUCKET_SHIFT``); never mutated once published."""

    __slots__ = ("_keys",)

    def sorted_keys(self) -> array:
        keys = getattr(self, "_keys", None)
        if keys is None:
            keys = array("Q", sorted(self))
            self._keys = keys
        return keys


class DictLayer:
    """Immutable, persistent mapping ``rid -> (vstart, vend)`` (point edits; the top layer of a state).

    Buckets keyed by ``rid >> 12`` hold small dicts; :meth:`with_changes` copies the bucket directory and
    only the touched buckets, so versions share everything else."""

    __slots__ = ("_bids", "_buckets", "_len")

    def __init__(self) -> None:
        self._buckets: dict[int, _Bucket] = {}
        self._len = 0
        self._bids: array | None = None

    @classmethod
    def _make(cls, buckets: dict[int, _Bucket], length: int) -> DictLayer:
        layer = cls.__new__(cls)
        layer._buckets = buckets
        layer._len = length
        layer._bids = None
        return layer

    @classmethod
    def from_items(cls, items: Iterable[tuple[int, tuple[int, int]]]) -> DictLayer:
        """A layer holding ``items`` (later duplicates win)."""
        return cls().with_changes(dict(items))

    # -- mapping API ------------------------------------------------------------------
    def get(self, rid: int) -> tuple[int, int] | None:
        b = self._buckets.get(rid >> BUCKET_SHIFT)
        return None if b is None else b.get(rid)

    def __contains__(self, rid: object) -> bool:
        if not isinstance(rid, int):
            return False
        b = self._buckets.get(rid >> BUCKET_SHIFT)
        return b is not None and rid in b

    def __len__(self) -> int:
        return self._len

    def __repr__(self) -> str:
        return f"DictLayer(len={self._len}, buckets={len(self._buckets)})"

    def _sorted_bids(self) -> array:
        bids = self._bids
        if bids is None:
            bids = array("Q", sorted(self._buckets))
            self._bids = bids
        return bids

    def keys_in(self, lo: int, hi: int) -> array:
        """Ascending rids in ``[lo, hi)`` as ``array('Q')``."""
        out = array("Q")
        lo = max(0, operator.index(lo))
        hi = operator.index(hi)
        if hi <= lo or not self._len:
            return out
        b0, b1 = lo >> BUCKET_SHIFT, (hi - 1) >> BUCKET_SHIFT
        buckets = self._buckets
        if b1 - b0 < 64:
            bids: Iterable[int] = range(b0, b1 + 1)
        else:
            sb = self._sorted_bids()
            bids = sb[bisect.bisect_left(sb, b0):bisect.bisect_right(sb, b1)]
        for bid in bids:
            bk = buckets.get(bid)
            if bk is None:
                continue
            keys = bk.sorted_keys()
            if bid == b0 or bid == b1:
                a = bisect.bisect_left(keys, lo) if bid == b0 else 0
                b = bisect.bisect_left(keys, hi) if bid == b1 else len(keys)
                if b > a:
                    out.extend(keys[a:b])
            else:
                out.extend(keys)
        return out

    def spans_in(self, lo: int, hi: int) -> tuple[array, array]:
        """(ascending rids in ``[lo, hi)``, their flat ``(start, end)`` pairs)."""
        keys = self.keys_in(lo, hi)
        return keys, array("Q", chain.from_iterable(map(self.get, keys)))

    def items(self) -> Iterator[tuple[int, tuple[int, int]]]:
        """``(rid, span)`` in ascending rid order."""
        buckets = self._buckets
        for bid in self._sorted_bids():
            bk = buckets[bid]
            for rid in bk.sorted_keys():
                yield rid, bk[rid]

    def with_changes(self, changes: Mapping[int, tuple[int, int] | None]) -> DictLayer:
        """A new layer with ``changes`` applied (``None`` removes the rid)."""
        if not changes:
            return self
        buckets = dict(self._buckets)
        length = self._len
        touched: dict[int, _Bucket] = {}
        for rid, value in changes.items():
            rid = _nonneg(rid, "rid")
            if rid > U64_MAX:
                raise ValueError(f"rid out of range: {rid}")
            bid = rid >> BUCKET_SHIFT
            b = touched.get(bid)
            if b is None:
                old = buckets.get(bid)
                b = _Bucket(old) if old is not None else _Bucket()
                touched[bid] = b
            if value is None:
                if rid in b:
                    del b[rid]
                    length -= 1
            else:
                span = _span(value)
                if rid not in b:
                    length += 1
                b[rid] = span
        for bid, b in touched.items():
            if b:
                buckets[bid] = b
            else:
                buckets.pop(bid, None)
        return DictLayer._make(buckets, length)

    # -- serialization (checkpoints) ---------------------------------------------------
    def to_bytes(self) -> bytes:
        """u64 LE ``(rid, vstart, vend)`` triples in ascending rid order."""
        out = array("Q")
        for rid, (s, e) in self.items():
            out.append(rid)
            out.append(s)
            out.append(e)
        return _le_bytes(out)

    @classmethod
    def from_bytes(cls, b: bytes | bytearray | memoryview) -> DictLayer:
        """Inverse of :meth:`to_bytes` (validated: ascending unique rids, ``vstart <= vend``)."""
        if len(b) % 24:
            raise ValueError("dict layer blob: length is not a multiple of 24")
        t = _from_le(b, "dict layer blob")
        rids, starts, ends = t[0::3], t[1::3], t[2::3]
        if not _strictly_ascending(rids) or not all(map(le, starts, ends)):
            raise ValueError("dict layer blob: invalid entries")
        buckets: dict[int, _Bucket] = {}
        for rid, s, e in zip(rids, starts, ends):
            bid = rid >> BUCKET_SHIFT
            bk = buckets.get(bid)
            if bk is None:
                bk = buckets[bid] = _Bucket()
            bk[rid] = (s, e)
        return cls._make(buckets, len(rids))


def _layer_header(flags: int, count: int, crc: int, segments: Sequence[int]) -> bytes:
    head = _LAYER_HEAD.pack(LAYER_MAGIC, LAYER_VERSION, flags, count, crc, len(segments))
    head += struct.pack(f"<{len(segments)}I", *segments)
    return head + bytes((-len(head)) % 64)


def _header_size(nsegs: int) -> int:
    n = _LAYER_HEAD.size + 4 * nsegs
    return n + (-n) % 64


_LAYERS_LOCK = threading.Lock()
_LAYERS: weakref.WeakSet[ArrayLayer] = weakref.WeakSet()


def array_layers_of(path: StrPath) -> list[ArrayLayer]:
    """The alive :class:`ArrayLayer` objects of this process mapping the file ``path`` (normalized paths)."""
    norm = normalize_path(path)
    with _LAYERS_LOCK:
        layers = list(_LAYERS)
    return [layer for layer in layers if normalize_path(layer.path) == norm]


class ArrayLayer:
    """Immutable, file-backed content layer (bulk results): sorted rids + spans, memory-mapped.

    Opened with :meth:`open`; the file stays mapped until :meth:`close` (it may be deleted meanwhile).
    Reads are thread-safe; ``close`` must not race with reads (the owning Document closes layers only after
    every lease ended). :attr:`owned` is set by ``Document.set_overlay`` (like :attr:`DatReader.owned`).

    :meth:`get` bisects the mapped rid array (a few pages per lookup); the range reads (:meth:`keys_in`,
    :meth:`spans_in`, :meth:`items`) of :data:`FILE_READ_MIN` or more values read the file through a handle of
    their own instead of slicing the mapping, so a pass over a layer of millions of entries does not add the
    whole file to the working set of the process (the OS file cache holds it); smaller ones slice the mapping
    (no system call per small range)."""

    __slots__ = ("__weakref__", "_f", "_flock", "_hs", "_mm", "_pairs", "_raw", "_rids", "count", "flags", "name",
                 "owned", "path", "segments")

    def __init__(self) -> None:
        raise TypeError("use ArrayLayer.open(path)")

    @classmethod
    def open(cls, path: StrPath) -> ArrayLayer:
        """Open and validate a layer file (header, count vs size, crc32) -> ``EditError`` when damaged."""
        p = Path(path)
        try:
            f = open_shared(p, "rb")
        except Exception as exc:  # localized JsonReshapeError (missing file, permission, ...)
            raise _layer_corrupt(p, str(exc)) from exc
        mm = None
        try:
            size = os.fstat(f.fileno()).st_size
            head = f.read(_LAYER_HEAD.size)
            if len(head) < _LAYER_HEAD.size:
                raise _layer_corrupt(p, "truncated header")
            magic, version, flags, count, crc, nsegs = _LAYER_HEAD.unpack(head)
            if magic != LAYER_MAGIC:
                raise _layer_corrupt(p, "not a layer file")
            if version != LAYER_VERSION:
                raise _layer_corrupt(p, f"unsupported layer version {version}")
            if nsegs > MAX_LAYER_SEGMENTS:
                raise _layer_corrupt(p, "invalid segment list")
            hs = _header_size(nsegs)
            if size != hs + 24 * count:
                raise _layer_corrupt(p, f"size {size} does not match {count} entries")
            rest = f.read(hs - _LAYER_HEAD.size)
            if len(rest) != hs - _LAYER_HEAD.size:
                raise _layer_corrupt(p, "truncated header")
            segments = struct.unpack_from(f"<{nsegs}I", rest, 0)
            if any(rest[4 * nsegs:]):
                raise _layer_corrupt(p, "invalid header padding")
            calc = 0
            remaining = 24 * count
            while remaining:
                chunk = f.read(min(_IO_CHUNK, remaining))
                if not chunk:
                    raise _layer_corrupt(p, "truncated data")
                calc = zlib.crc32(chunk, calc)
                remaining -= len(chunk)
            if calc != crc:
                raise _layer_corrupt(p, "checksum mismatch")
            layer = cls.__new__(cls)
            layer.path = p
            layer.name = p.name
            layer.count = count
            layer.flags = flags
            layer.segments = tuple(segments)
            layer._hs = hs
            layer.owned = False
            with _LAYERS_LOCK:
                _LAYERS.add(layer)
            if _BIG_ENDIAN:  # pragma: no cover - mmap views assume little-endian u64
                f.seek(hs)
                data = f.read(24 * count)
                layer._mm = None
                layer._raw = None
                layer._f = None
                layer._flock = None
                layer._rids = _from_le(data[:8 * count])
                layer._pairs = _from_le(data[8 * count:])
                return layer
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            raw = memoryview(mm)
            layer._mm = mm
            layer._raw = raw
            layer._rids = raw[hs:hs + 8 * count].cast("Q")
            layer._pairs = raw[hs + 8 * count:hs + 24 * count].cast("Q")
            layer._f = open_shared(p, "rb", buffering=0)
            layer._flock = threading.Lock()
            mm = None
            return layer
        except OSError as exc:
            raise _layer_corrupt(p, str(exc)) from exc
        finally:
            if mm is not None:
                mm.close()
            f.close()  # the mapping keeps its own (shared-delete) handle

    # -- access --------------------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._rids is None

    def _views(self) -> tuple[Any, Any]:
        rids, pairs = self._rids, self._pairs
        if rids is None or pairs is None:
            raise ValueError(f"array layer {self.name} is closed")
        return rids, pairs

    def __len__(self) -> int:
        return self.count

    def __repr__(self) -> str:
        return f"ArrayLayer({self.name!r}, count={self.count}, closed={self.closed})"

    def get(self, rid: int) -> tuple[int, int] | None:
        """Span of ``rid`` or ``None`` (bisect on the mapped rid array; no RAM index)."""
        rids, pairs = self._views()
        i = bisect.bisect_left(rids, rid)
        if i < self.count and rids[i] == rid:
            return pairs[2 * i], pairs[2 * i + 1]
        return None

    def __contains__(self, rid: object) -> bool:
        return isinstance(rid, int) and self.get(rid) is not None

    def _bounds(self, lo: int, hi: int) -> tuple[int, int]:
        rids, _pairs = self._views()
        lo = max(0, operator.index(lo))
        hi = operator.index(hi)
        if hi <= lo:
            return 0, 0
        a = bisect.bisect_left(rids, lo)
        return a, bisect.bisect_left(rids, hi, a)

    def _read_q(self, off: int, n: int) -> array:
        """``n`` u64 values at byte offset ``off`` of the file: fewer than :data:`FILE_READ_MIN` values are sliced
        from the mapping (a few pages; no system call), more are read through the layer's own handle (a pass over
        a big layer must not add the whole file to the working set)."""
        if n < FILE_READ_MIN:
            raw = self._raw
            if raw is None:
                raise ValueError(f"array layer {self.name} is closed")
            out = array("Q")
            out.frombytes(raw[off:off + 8 * n])
            return out
        out = array("Q", bytes(8 * n))
        view = memoryview(out).cast("B")
        lock, f = self._flock, self._f
        if lock is None or f is None:
            raise ValueError(f"array layer {self.name} is closed")
        got = 0
        try:
            with lock:
                f.seek(off)
                while got < 8 * n:
                    k = f.readinto(view[got:])
                    if not k:
                        break
                    got += k
        except (OSError, ValueError) as exc:
            raise _layer_corrupt(self.path, str(exc)) from exc
        finally:
            view.release()
        if got != 8 * n:
            raise _layer_corrupt(self.path, f"short read at {off}")
        return out

    def keys_in(self, lo: int, hi: int) -> array:
        """Ascending rids in ``[lo, hi)`` (read from the file)."""
        a, b = self._bounds(lo, hi)
        if b <= a:
            return array("Q")
        if self._raw is None:  # pragma: no cover - big-endian hosts
            return self._rids[a:b]
        return self._read_q(self._hs + 8 * a, b - a)

    def spans_in(self, lo: int, hi: int) -> tuple[array, array]:
        """(rids in ``[lo, hi)``, their flat ``(start, end)`` pairs), read from the file."""
        a, b = self._bounds(lo, hi)
        if b <= a:
            return array("Q"), array("Q")
        if self._raw is None:  # pragma: no cover - big-endian hosts
            return self._rids[a:b], self._pairs[2 * a:2 * b]
        hs, n = self._hs, self.count
        return self._read_q(hs + 8 * a, b - a), self._read_q(hs + 8 * n + 16 * a, 2 * (b - a))

    def items(self) -> Iterator[tuple[int, tuple[int, int]]]:
        """``(rid, span)`` in ascending rid order (read in blocks)."""
        n = self.count
        hs = self._hs
        for a in range(0, n, _BLOCK):
            b = min(n, a + _BLOCK)
            rids, pairs = self._views()
            if self._raw is None:  # pragma: no cover - big-endian hosts
                keys, flat = rids[a:b], pairs[2 * a:2 * b]
            else:
                keys = self._read_q(hs + 8 * a, b - a)
                flat = self._read_q(hs + 8 * n + 16 * a, 2 * (b - a))
            yield from zip(keys, zip(flat[0::2], flat[1::2]))

    def close(self) -> None:
        """Release the mapping and the handle (idempotent). Waits briefly for a concurrent slice of the
        mapping to be released."""
        rids, pairs, raw, mm = self._rids, self._pairs, self._raw, self._mm
        self._rids = self._pairs = self._raw = self._mm = None
        f, self._f = self._f, None
        if f is not None:
            lock = self._flock
            if lock is not None:
                with lock:
                    f.close()
            else:  # pragma: no cover
                f.close()
        for view in (rids, pairs, raw):
            if view is not None:
                view.release()
        if mm is not None:
            deadline = time.monotonic() + CLOSE_WAIT
            while True:
                try:
                    mm.close()
                    break
                except BufferError:  # a reader still holds a slice of the mapping
                    if time.monotonic() > deadline:
                        log.warning("array layer %s: mapping still in use; left to the garbage collector",
                                    self.name)
                        break
                    time.sleep(0.005)


# ----------------------------------------------------------------------------------
# write_array_layer
# ----------------------------------------------------------------------------------
class EntryBlocks:
    """Fast input for :func:`write_array_layer`: the entries as blocks of three parallel sequences
    ``(rids, vstarts, vends)`` (ideally ``array('Q')``) instead of one tuple per entry -- no per-entry Python
    for producers that already hold arrays (e.g. duplicating a big selection)."""

    __slots__ = ("blocks",)

    def __init__(self, blocks: Iterable[tuple[Any, Any, Any]]) -> None:
        self.blocks = blocks


def _array_blocks(blocks: Iterable[tuple[Any, Any, Any]], cancel: Any) -> Iterator[tuple[array, array, array]]:
    for blk in blocks:
        try:
            r, s, e = blk
        except (TypeError, ValueError) as exc:
            raise ValueError("EntryBlocks items must be (rids, vstarts, vends)") from exc
        r, s, e = _u64(r, "rids"), _u64(s, "vstarts"), _u64(e, "vends")
        if not len(r) == len(s) == len(e):
            raise ValueError("EntryBlocks: rids, vstarts and vends differ in length")
        if not all(map(le, s, e)):
            raise ValueError("array layer entry with vstart > vend")
        for a in range(0, len(r), _BLOCK):      # bounded pieces: SPILL_ENTRIES keeps bounding the batch
            if cancel is not None:
                cancel.check()
            yield r[a:a + _BLOCK], s[a:a + _BLOCK], e[a:a + _BLOCK]


class _Batch:
    """Entry blocks collected for one batch; :meth:`take` builds each column with ONE allocation (growing an
    array block by block re-copies it dozens of times: CPython over-allocates arrays by only 1/16)."""

    __slots__ = ("cols", "n")

    def __init__(self) -> None:
        self.cols: tuple[list[array | None], list[array | None], list[array | None]] = ([], [], [])
        self.n = 0

    def add(self, r: array, s: array, e: array) -> None:
        for col, a in zip(self.cols, (r, s, e)):
            col.append(a)
        self.n += len(r)

    def take(self) -> tuple[array, array, array]:
        out = []
        for col in self.cols:
            a = array("Q", [0]) * self.n if self.n else array("Q")
            pos = 0
            for i, blk in enumerate(col):
                assert blk is not None
                a[pos:pos + len(blk)] = blk
                pos += len(blk)
                col[i] = None                 # free the block as soon as it is copied
            col.clear()
            out.append(a)
        self.n = 0
        return out[0], out[1], out[2]


def _entry_blocks(entries: Iterable[tuple[int, int, int]], cancel: Any) -> Iterator[tuple[array, array, array]]:
    it = iter(entries)
    while True:
        block = list(islice(it, _BLOCK))
        if not block:
            return
        if cancel is not None:
            cancel.check()
        try:
            r = array("Q", map(itemgetter(0), block))
            s = array("Q", map(itemgetter(1), block))
            e = array("Q", map(itemgetter(2), block))
        except (TypeError, OverflowError, IndexError, ValueError) as exc:
            raise ValueError("array layer entries must be (rid, vstart, vend) unsigned integers") from exc
        if not all(map(le, s, e)):
            raise ValueError("array layer entry with vstart > vend")
        yield r, s, e


def _natural_bounds(r: array) -> list[int] | None:
    """Bounds ``[0, b1, ..., len(r)]`` of the maximal non-decreasing runs of ``r``, or ``None`` when there
    are more than MERGE_RUNS_MAX runs (the scan stops there: random input never builds a list of breaks)."""
    n = len(r)
    breaks = list(islice(compress(range(1, n), map(gt, r, islice(r, 1, None))), MERGE_RUNS_MAX))
    if len(breaks) >= MERGE_RUNS_MAX:
        return None
    return [0, *breaks, n]


def _dedupe_last(r: array, s: array, e: array) -> tuple[array, array, array]:
    """Sorted (stable) arrays -> keep the LAST entry of every rid."""
    n = len(r)
    dups = list(compress(range(1, n), map(eq, r, islice(r, 1, None))))
    if not dups:
        return r, s, e
    keep = bytearray(b"\x01") * n
    for i in dups:
        keep[i - 1] = 0
    return array("Q", compress(r, keep)), array("Q", compress(s, keep)), array("Q", compress(e, keep))


def _remove_quiet(p: Path) -> None:
    try:
        os.remove(long_path(p))
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("cannot remove temporary file %s", p, exc_info=True)


def _fsync_dir(directory: Path) -> None:
    if sys.platform == "win32":  # NTFS: the rename is journaled; directories cannot be fsynced
        return
    try:  # pragma: no cover - POSIX only
        fd = os.open(long_path(directory), os.O_RDONLY)
    except OSError:  # pragma: no cover
        return
    try:  # pragma: no cover
        os.fsync(fd)
    except OSError:  # pragma: no cover
        pass
    finally:  # pragma: no cover
        os.close(fd)


def _temp_name(directory: Path, suffix: str, temps: list[Path]) -> Path:
    """A new temporary file name in ``directory``, registered in ``temps`` (removed at the end)."""
    path = directory / f"jrlayer-{os.getpid()}-{secrets.token_hex(6)}{suffix}"
    temps.append(path)
    return path


def _write_u64(f: BinaryIO, a: array, crc: int = 0) -> int:
    """Write ``a`` as u64 LE (no copy on little-endian hosts); returns the crc32 continued over it."""
    if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only
        a = array("Q", a)
        a.byteswap()
    f.write(a)  # type: ignore[arg-type]  # arrays are bytes-like
    return zlib.crc32(a, crc)


def _write_strided(f: BinaryIO, cols: Sequence[array], crc: int = 0, lo: int = 0, hi: int | None = None) -> int:
    """Write the columns interleaved (``c0[i], c1[i], ...``) in blocks -- entries ``lo .. hi - 1`` (default:
    all; no copy of the range) --; returns the continued crc32."""
    n, k = len(cols[0]) if hi is None else hi, len(cols)
    for a in range(lo, n, _BLOCK):
        b = min(n, a + _BLOCK)
        blk = array("Q", bytes(8 * k * (b - a)))
        for j, col in enumerate(cols):
            blk[j::k] = col[a:b]
        crc = _write_u64(f, blk, crc)
    return crc


_Run = tuple[Path, int, int]   # run file, first rid, last rid


def _spill(r: array, s: array, e: array, work: Path, temps: list[Path], lo: int = 0, hi: int | None = None) -> _Run:
    """Write one sorted, non-empty run -- entries ``lo .. hi - 1`` of the arrays (default: all) -- as
    ``(rid, vstart, vend)`` triples to a temporary run file."""
    hi = len(r) if hi is None else hi
    path = _temp_name(work, ".run", temps)
    with open_shared(path, "wb") as f:
        _write_strided(f, (r, s, e), 0, lo, hi)
    return path, r[lo], r[hi - 1]


def _batch_runs(r: array, s: array, e: array, work: Path, temps: list[Path], cancel: Any) -> list[_Run]:
    """A batch -> sorted run files: natural runs are written as they are (C-level slices of blocks, no
    per-entry Python, no copy of a run); more than MERGE_RUNS_MAX runs -> stable sorts of SORT_BLOCK entries,
    one file each."""
    bounds = _natural_bounds(r)
    out: list[_Run] = []
    if bounds is not None:
        for a, b in zip(bounds, islice(bounds, 1, None)):
            if b > a:
                out.append(_spill(r, s, e, work, temps, a, b))
        return out
    n = len(r)
    for a in range(0, n, SORT_BLOCK):
        if cancel is not None:
            cancel.check()
        b = min(n, a + SORT_BLOCK)
        order = sorted(range(a, b), key=r.__getitem__)          # stable: duplicates keep input order
        out.append(_spill(array("Q", map(r.__getitem__, order)), array("Q", map(s.__getitem__, order)),
                          array("Q", map(e.__getitem__, order)), work, temps))
        del order
    return out


def _read_run(path: Path, fanin: int = 1) -> Iterator[tuple[int, int, int]]:
    """Stream the triples of a run file; read buffers stay within MERGE_BUFFER over ``fanin`` files."""
    per = max(1024, min(_BLOCK, MERGE_BUFFER // (24 * max(1, fanin))))
    with open_shared(path, "rb") as f:
        while True:
            data = f.read(24 * per)
            if not data:
                return
            it = iter(_from_le(data, "run file"))
            del data
            yield from zip(it, it, it)


def _reduce_runs(runs: list[_Run], work: Path, temps: list[Path], cancel: Any) -> list[_Run]:
    """Merge consecutive groups of MERGE_FANIN run files (stable, input order kept) until at most
    MERGE_FANIN remain."""
    while len(runs) > MERGE_FANIN:
        nxt: list[_Run] = []
        for i in range(0, len(runs), MERGE_FANIN):
            group = runs[i:i + MERGE_FANIN]
            if len(group) == 1:
                nxt.append(group[0])
                continue
            out = _temp_name(work, ".run", temps)
            with open_shared(out, "wb") as f:
                buf = array("Q")
                merged = heapq.merge(*(_read_run(p, len(group)) for p, _a, _b in group), key=itemgetter(0))
                for item in merged:
                    buf.extend(item)
                    if len(buf) >= 3 * _BLOCK:
                        _write_u64(f, buf)
                        del buf[:]
                        if cancel is not None:
                            cancel.check()
                _write_u64(f, buf)
            for p, _a, _b in group:
                _remove_quiet(p)
            nxt.append((out, min(g[1] for g in group), max(g[2] for g in group)))
        runs = nxt
    return runs


def _finish_layer(out: BinaryIO, pf: BinaryIO, crc: int, count: int, flags: int, segs: tuple[int, ...]) -> None:
    """Append the pairs collected in ``pf`` to ``out`` (continuing the crc), write the header, fsync."""
    pf.flush()
    pf.seek(0)
    while True:
        chunk = pf.read(_IO_CHUNK)
        if not chunk:
            break
        crc = zlib.crc32(chunk, crc)
        out.write(chunk)
    out.seek(0)
    out.write(_layer_header(flags, count, crc, segs))
    out.flush()
    os.fsync(out.fileno())


def _write_concat(tmp: Path, runs: list[_Run], work: Path, temps: list[Path], flags: int,
                  segs: tuple[int, ...], cancel: Any) -> int:
    """Run files that are already in global rid order -> the layer file, block by block at C speed
    (no per-entry Python). Equal rids at block / file boundaries: the later entry wins."""
    pairs_path = _temp_name(work, ".pairs", temps)
    count = 0
    crc = 0
    with open_shared(tmp, "w+b") as out, open_shared(pairs_path, "w+b") as pf:
        out.write(bytes(_header_size(len(segs))))
        held: tuple[int, int, int] | None = None     # last entry of the previous block, written later

        def put_held() -> None:
            nonlocal crc, count
            assert held is not None
            crc = _write_u64(out, array("Q", held[:1]), crc)
            _write_u64(pf, array("Q", held[1:]))
            count += 1

        for path, _first, _last in runs:
            with open_shared(path, "rb") as f:
                while True:
                    data = f.read(24 * _BLOCK)
                    if not data:
                        break
                    t = _from_le(data, "run file")
                    del data
                    r, s, e = _dedupe_last(t[0::3], t[1::3], t[2::3])
                    del t
                    if held is not None and held[0] != r[0]:
                        put_held()
                    held = (r.pop(), s.pop(), e.pop())
                    if r:
                        crc = _write_u64(out, r, crc)
                        _write_strided(pf, (s, e))
                        count += len(r)
                    if cancel is not None:
                        cancel.check()
        if held is not None:
            put_held()
        _finish_layer(out, pf, crc, count, flags, segs)
    return count


def _replace(src: Path, dst: Path) -> None:
    deadline = time.monotonic() + 1.0
    while True:
        try:
            os.replace(long_path(src), long_path(dst))
            return
        except PermissionError:
            if sys.platform != "win32" or time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def write_array_layer(path: StrPath, entries: Iterable[tuple[int, int, int]] | EntryBlocks, *, flags: int,
                      segments: Iterable[int], work_dir: StrPath | None, cancel: Any = None) -> int:
    """Write an array layer file from ``(rid, vstart, vend)`` entries in ANY order (or an
    :class:`EntryBlocks` of parallel arrays); returns the count.

    Entries are collected in batches of up to :data:`SPILL_ENTRIES`. A single batch that is already sorted
    is written directly; one with at most :data:`MERGE_RUNS_MAX` natural ascending runs is merged with
    ``heapq.merge`` straight from memory into the file. Otherwise (more runs, or more than one batch) every
    batch becomes sorted run files in ``work_dir`` (default: the target folder; natural runs written as they
    are, else stable block sorts of :data:`SORT_BLOCK` entries) and the batch is freed; run files that are
    already in global order (sorted input of any size) are concatenated block by block, others are merged
    from disk (at most :data:`MERGE_FANIN` files at once). Duplicate rids: the LAST entry wins. The file is
    written as ``<path>.tmp``, fsynced and renamed; temporary files are always removed. ``cancel``
    (optional ``CancelToken``) is checked per block. Memory: about 24 bytes per batch entry plus a block
    sort (SORT_BLOCK entries) and bounded read buffers."""
    target = Path(path)
    flags = operator.index(flags)
    if not 0 <= flags <= 0xFFFFFFFF:
        raise ValueError(f"flags out of range: {flags}")
    segs = tuple(operator.index(sg) for sg in segments)
    if any(not 0 <= sg <= min(MAX_SEGMENT, 0xFFFFFFFF) for sg in segs) or len(segs) > MAX_LAYER_SEGMENTS:
        raise ValueError(f"invalid segment list: {segs[:10]!r}")
    work = Path(work_dir) if work_dir is not None else target.parent
    temps: list[Path] = []
    runs: list[_Run] = []
    tmp = target.with_name(target.name + ".tmp")
    batch = _Batch()
    source = (_array_blocks(entries.blocks, cancel) if isinstance(entries, EntryBlocks)
              else _entry_blocks(entries, cancel))
    try:
        for r, s, e in source:
            batch.add(r, s, e)
            if batch.n >= SPILL_ENTRIES:
                make_dirs(work)
                runs.extend(_batch_runs(*batch.take(), work, temps, cancel))
        br, bs, be = batch.take()
        count: int | None = None
        if not runs:
            bounds = _natural_bounds(br)
            if bounds is not None and len(bounds) <= 2:            # sorted (or empty): direct write
                r, s, e = _dedupe_last(br, bs, be)
                with open_shared(tmp, "wb") as f:
                    f.write(bytes(_header_size(len(segs))))
                    crc = _write_u64(f, r)
                    crc = _write_strided(f, (s, e), crc)
                    f.seek(0)
                    f.write(_layer_header(flags, len(r), crc, segs))
                    f.flush()
                    os.fsync(f.fileno())
                count = len(r)
            elif bounds is not None:                                # few natural runs: merge from memory
                mr, ms, me = memoryview(br), memoryview(bs), memoryview(be)
                sources = [zip(mr[a:b], ms[a:b], me[a:b]) for a, b in zip(bounds, islice(bounds, 1, None))]
                make_dirs(work)
                count = _write_merged(tmp, sources, work, temps, flags, segs, cancel)
                del sources
            else:
                make_dirs(work)
                runs = _batch_runs(br, bs, be, work, temps, cancel)
        elif br:
            runs.extend(_batch_runs(br, bs, be, work, temps, cancel))
        if count is None:
            del br, bs, be                                          # the batch is on disk now
            if all(runs[i][1] >= runs[i - 1][2] for i in range(1, len(runs))):
                count = _write_concat(tmp, runs, work, temps, flags, segs, cancel)
            else:
                runs = _reduce_runs(runs, work, temps, cancel)
                count = _write_merged(tmp, [_read_run(p, len(runs)) for p, _a, _b in runs], work, temps, flags,
                                      segs, cancel)
        _replace(tmp, target)
        _fsync_dir(target.parent)
        return count
    except BaseException:
        _remove_quiet(tmp)
        raise
    finally:
        for p in temps:
            _remove_quiet(p)


def _write_merged(tmp: Path, sources: Sequence[Iterator[tuple[int, int, int]]], work: Path, temps: list[Path],
                  flags: int, segs: tuple[int, ...], cancel: Any) -> int:
    """k-way merge of sorted ``(rid, vstart, vend)`` streams (stable: on equal rids the later source and
    the later entry win) into the layer file. Rids go straight to the file, pairs through a temporary file
    appended at the end (the count is only known after the duplicates are dropped)."""
    pairs_path = _temp_name(work, ".pairs", temps)
    hs = _header_size(len(segs))
    count = 0
    crc = 0
    with open_shared(tmp, "w+b") as out, open_shared(pairs_path, "w+b") as pf:
        out.write(bytes(hs))
        rbuf, pbuf = array("Q"), array("Q")

        def flush() -> None:
            nonlocal crc
            crc = _write_u64(out, rbuf, crc)
            _write_u64(pf, pbuf)
            del rbuf[:], pbuf[:]

        prev: tuple[int, int, int] | None = None
        for item in heapq.merge(*sources, key=itemgetter(0)):
            if prev is not None:
                if item[0] == prev[0]:
                    prev = item
                    continue
                rbuf.append(prev[0])
                pbuf.append(prev[1])
                pbuf.append(prev[2])
                count += 1
                if len(rbuf) >= _BLOCK:
                    flush()
                    if cancel is not None:
                        cancel.check()
            prev = item
        if prev is not None:
            rbuf.append(prev[0])
            pbuf.append(prev[1])
            pbuf.append(prev[2])
            count += 1
        flush()
        _finish_layer(out, pf, crc, count, flags, segs)
    return count


# ----------------------------------------------------------------------------------
# DatReader
# ----------------------------------------------------------------------------------
_READERS_LOCK = threading.Lock()
_READERS: weakref.WeakSet[DatReader] = weakref.WeakSet()
_READER_UIDS = itertools.count(1)
_READER_CLOSED_HOOK: Callable[[], None] | None = None


def readers_of(folder: StrPath) -> list[DatReader]:
    """The alive (not garbage-collected) :class:`DatReader` objects of this process that read the segments of
    ``folder`` (compared as normalized paths), open or closed."""
    norm = normalize_path(folder)
    with _READERS_LOCK:
        readers = list(_READERS)
    return [r for r in readers if r._norm_dir() == norm]


def set_reader_closed_hook(hook: Callable[[], None] | None) -> None:
    """Install the callable :meth:`DatReader.close` calls after closing (``None`` removes it). It runs in the
    closing thread, so it must not block (the edit store only wakes its file reaper with it)."""
    global _READER_CLOSED_HOOK
    _READER_CLOSED_HOOK = hook


class DatReader:
    """Thread-safe reads of a layer's segment files (``seg-%05d.dat`` in ``dir``).

    Each thread reads through its own unbuffered :func:`~jsonreshape.fileio.open_shared` handle per segment
    (``threading.local``; handles of finished threads are closed when the next thread registers and by
    :meth:`close`). Only bytes made visible with :meth:`publish` can be read. :attr:`uid` is unique in the
    process (never reused, unlike ``id()``); :attr:`owned` is set by ``Document.set_overlay`` when a document
    takes the reader over (it closes it): the edit store then keeps the files states of this reader may still
    read until they are gone."""

    def __init__(self, dir: StrPath) -> None:  # noqa: A002 - spec name
        self.dir = Path(dir)
        self._lock = threading.Lock()
        self._ends: dict[int, int] = {}
        self._local = threading.local()
        self._registry: list[tuple[weakref.ref[threading.Thread], dict[int, BinaryIO]]] = []
        self._closed = False
        self._norm: tuple[Path, str] | None = None
        self.owned = False
        with _READERS_LOCK:
            self.uid = next(_READER_UIDS)
            _READERS.add(self)

    def __repr__(self) -> str:
        return f"DatReader({str(self.dir)!r}, segments={len(self._ends)}, closed={self._closed})"

    def _norm_dir(self) -> str:
        """``normalize_path(self.dir)``, cached per value of ``dir`` (the store may move a planned layer)."""
        d = self.dir
        cached = self._norm
        if cached is None or cached[0] is not d:
            cached = (d, normalize_path(d))
            self._norm = cached
        return cached[1]

    @property
    def closed(self) -> bool:
        return self._closed

    def segment_path(self, seg: int) -> Path:
        return self.dir / segment_name(seg)

    def publish(self, seg: int, end: int) -> None:
        """Make bytes ``[0, end)`` of segment ``seg`` readable (call after they are written and synced,
        before any state references them). Monotonic: a lower end raises ``ValueError``."""
        seg, end = operator.index(seg), operator.index(end)
        if not 0 <= seg <= MAX_SEGMENT:
            raise ValueError(f"segment number out of range: {seg}")
        if not 0 <= end < SEG_SIZE_LIMIT:
            raise ValueError(f"segment end out of range: {end}")
        with self._lock:
            if end < self._ends.get(seg, 0):
                raise ValueError(f"published end of segment {seg} cannot shrink ({self._ends[seg]} -> {end})")
            self._ends[seg] = end

    def published(self, seg: int) -> int:
        """Published size of ``seg`` (0 when unknown)."""
        return self._ends.get(seg, 0)

    def segments(self) -> tuple[int, ...]:
        """Segments with a published end, ascending."""
        return tuple(sorted(self._ends))

    def forget(self, seg: int) -> None:
        """Drop a discarded segment: its bytes become unreadable and every thread's handle on it is closed."""
        with self._lock:
            self._ends.pop(seg, None)
            for _ref, handles in self._registry:
                f = handles.pop(seg, None)
                if f is not None:
                    f.close()

    def _prune_locked(self) -> None:
        alive = []
        for ref, handles in self._registry:
            th = ref()
            if th is not None and th.is_alive():
                alive.append((ref, handles))
            else:
                for f in list(handles.values()):
                    f.close()
                handles.clear()
        self._registry = alive

    def _handle(self, seg: int) -> BinaryIO:
        handles: dict[int, BinaryIO] | None = getattr(self._local, "handles", None)
        if handles is None:
            handles = {}
            with self._lock:
                if self._closed:
                    raise ValueError("DatReader is closed")
                self._prune_locked()
                self._registry.append((weakref.ref(threading.current_thread()), handles))
            self._local.handles = handles
        f = handles.get(seg)
        if f is None or f.closed:
            path = self.segment_path(seg)
            try:
                f = open_shared(path, "rb", buffering=0)
            except Exception as exc:  # localized JsonReshapeError (missing segment, ...)
                raise _layer_corrupt(path, str(exc)) from exc
            handles[seg] = f
        return f

    def read(self, vstart: int, vend: int) -> bytes:
        """Bytes of the virtual span ``[vstart, vend)`` (one segment, inside its published end)."""
        if self._closed:
            raise ValueError("DatReader is closed")
        try:
            seg, off = split_edit_offset(vstart)
        except (ValueError, TypeError) as exc:
            raise _layer_corrupt(self.dir, f"invalid edit span ({vstart}, {vend})") from exc
        end = vend - (vstart - off)
        published = self._ends.get(seg)
        if published is None or not off <= end <= published:
            raise _layer_corrupt(self.segment_path(seg),
                                 f"span {off}..{end} outside the published {published or 0} bytes")
        n = end - off
        if not n:
            return b""
        f = self._handle(seg)
        try:
            f.seek(off)
            data = f.read(n)
            if data is not None and len(data) == n:
                return data
            parts = [data or b""]
            got = len(parts[0])
            while got < n:
                more = f.read(n - got)
                if not more:
                    break
                parts.append(more)
                got += len(more)
        except OSError as exc:
            raise _layer_corrupt(self.segment_path(seg), str(exc)) from exc
        if got < n:
            raise _layer_corrupt(self.segment_path(seg), f"segment shorter than {end} bytes")
        return b"".join(parts)

    def open_segment(self, seg: int) -> BinaryIO:
        """A new read-only shared handle on segment ``seg`` (the caller owns and closes it; it is not
        limited to the published end)."""
        path = self.segment_path(seg)
        try:
            return open_shared(path, "rb")
        except Exception as exc:
            raise _layer_corrupt(path, str(exc)) from exc

    def close(self) -> None:
        """Close every handle (idempotent); later reads raise ``ValueError``. The closed-reader hook
        (:func:`set_reader_closed_hook`) is called afterwards."""
        with self._lock:
            was_closed = self._closed
            self._closed = True
            registry, self._registry = self._registry, []
            for _ref, handles in registry:
                for f in list(handles.values()):
                    try:
                        f.close()
                    except OSError:  # pragma: no cover
                        pass
                handles.clear()
        hook = _READER_CLOSED_HOOK
        if hook is not None and not was_closed:
            try:
                hook()
            except Exception:  # pragma: no cover - the hook only wakes a thread
                log.debug("the closed-reader hook failed", exc_info=True)


# ----------------------------------------------------------------------------------
# Remap
# ----------------------------------------------------------------------------------
_OP_KINDS = ("ins", "del", "inss", "dels", "move", "unmove", "pieces")


def _norm_op(op: Any) -> tuple | None:
    if not isinstance(op, (tuple, list)) or not op:
        raise ValueError(f"invalid remap op: {op!r}")
    kind = op[0]
    if kind in ("ins", "del"):
        if len(op) != 3:
            raise ValueError(f"remap op {kind!r} takes (pos, n)")
        pos, n = _nonneg(op[1], "pos"), _nonneg(op[2], "n")
        return (kind, pos, n) if n else None
    if kind in ("inss", "dels"):
        if len(op) != 2:
            raise ValueError(f"remap op {kind!r} takes (positions,)")
        a = _positions(op[1])
        return (kind, a) if a else None
    if kind in ("move", "unmove"):
        if len(op) != 3:
            raise ValueError(f"remap op {kind!r} takes 2 arguments")
        if kind == "move":
            a, to = _positions(op[1]), _nonneg(op[2], "to")
        else:
            to, a = _nonneg(op[1], "to"), _positions(op[2])
        if not a or (a[0] == to and a[-1] - a[0] + 1 == len(a)):
            return None  # moving nothing / a block onto itself
        return (kind, a, to) if kind == "move" else (kind, to, a)
    if kind == "pieces":
        if len(op) != 3 or not isinstance(op[1], PieceTable) or not isinstance(op[2], PieceTable):
            raise ValueError("remap op 'pieces' takes (old PieceTable, new PieceTable)")
        return None if op[1] is op[2] else ("pieces", op[1], op[2])
    raise ValueError(f"unknown remap op {kind!r}")


def _map_ins(x: array, asc: bool, s: int, n: int) -> tuple[array, bool]:
    if asc:
        j = bisect.bisect_left(x, s)
        out = x[:j]
        out.extend(map(add, x[j:], repeat(n)))
        return out, True
    return array("Q", map(add, x, map(mul, map(ge, x, repeat(s)), repeat(n)))), False


def _map_del(x: array, asc: bool, s: int, n: int) -> tuple[array, bool]:
    e = s + n
    if asc:
        j1 = bisect.bisect_left(x, s)
        j2 = bisect.bisect_left(x, e, j1)
        out = x[:j1]
        out.extend(map(sub, x[j2:], repeat(n)))
        return out, True
    keep = bytes(map(or_, map(lt, x, repeat(s)), map(ge, x, repeat(e))))
    shifted = map(sub, x, map(mul, map(ge, x, repeat(e)), repeat(n)))
    return array("Q", compress(shifted, keep)), False


def _shift_extend(out: array, seg: array, shift: int) -> None:
    if not seg:
        return
    if shift > 0:
        out.extend(map(add, seg, repeat(shift)))
    elif shift < 0:
        out.extend(map(sub, seg, repeat(-shift)))
    else:
        out.extend(seg)


def _map_inss(x: array, asc: bool, g: array) -> tuple[array, bool]:
    if asc:
        j0 = bisect.bisect_right(g, x[0])
        j1 = bisect.bisect_right(g, x[-1], j0)
        if (j1 - j0) * 4 <= len(x) + 16:   # merge: O(n + k) on the overlapping part of g
            out = array("Q")
            i = 0
            shift = j0
            for j in range(j0, j1):
                b = bisect.bisect_left(x, g[j], i)
                _shift_extend(out, x[i:b], shift)
                i = b
                shift += 1
            _shift_extend(out, x[i:], shift)
            return out, True
    return array("Q", map(add, x, map(bisect.bisect_right, repeat(g), x))), asc


def _map_dels(x: array, asc: bool, d: array, dx: array) -> tuple[array, bool]:
    if asc:
        j0 = bisect.bisect_left(d, x[0])
        j1 = bisect.bisect_right(d, x[-1], j0)
        if (j1 - j0) * 4 <= len(x) + 16:
            out = array("Q")
            i = 0
            shift = j0
            for j in range(j0, j1):
                v = d[j]
                b = bisect.bisect_left(x, v, i)
                _shift_extend(out, x[i:b], -shift)
                i = bisect.bisect_right(x, v, b)
                shift += 1
            _shift_extend(out, x[i:], -shift)
            return out, True
    r = array("Q", map(bisect.bisect_left, repeat(dx), x))
    keep = bytes(map(ne, map(dx.__getitem__, r), x))
    return array("Q", compress(map(sub, x, r), keep)), asc


def _map_move(x: array, p: array, px: array, to: int) -> array:
    k = len(p)
    r = array("Q", map(bisect.bisect_left, repeat(px), x))
    hit = bytes(map(eq, map(px.__getitem__, r), x))
    q = array("Q", map(sub, x, r))
    moved = map(add, r, repeat(to))
    others = map(add, q, map(mul, map(ge, q, repeat(to)), repeat(k)))
    return array("Q", map(add, map(mul, hit, moved), map(mul, map(not_, hit), others)))


def _map_unmove(x: array, to: int, p: array, g: array) -> array:
    k = len(p)
    end = to + k
    inside = bytes(map(and_, map(ge, x, repeat(to)), map(lt, x, repeat(end))))
    pv = map(p.__getitem__, map(mul, inside, map(sub, x, repeat(to))))
    q = array("Q", map(sub, x, map(mul, map(ge, x, repeat(end)), repeat(k))))
    ov = map(add, q, map(bisect.bisect_right, repeat(g), q))
    return array("Q", map(add, map(mul, inside, pv), map(mul, map(not_, inside), ov)))


def _map_pieces(x: array, old: PieceTable, new: PieceTable) -> array:
    rids = old.rids_at(x)
    pos, keep = new._pos_of_array(rids)
    if all(keep):
        return pos
    return array("Q", compress(pos, keep))


class Remap:
    """Old position -> new position (records, or the children of one container) for one step.

    Ops are applied in order, each in the coordinates left by the previous one:

    * ``("ins", pos, n)``: n new items at pos;  ``("del", pos, n)``: n items removed at pos;
    * ``("inss", positions)``: new items at these ascending NEW positions;
    * ``("dels", positions)``: the items at these ascending OLD positions removed;
    * ``("move", positions, to)``: :meth:`PieceTable.move` semantics; ``("unmove", to, positions)``: its inverse;
    * ``("pieces", old, new)``: general: ``map(p) = new.pos_of(old.rid_at(p))``.

    Op arrays are ``array('Q')`` copies (never mutate them). No-op ops (``n == 0``, empty arrays, a block
    moved onto itself, identical tables) are dropped, so :attr:`identity` means "no position changes".
    """

    __slots__ = ("_aux", "_ops")

    def __init__(self, ops: Sequence[Any] = ()) -> None:
        norm = []
        for op in ops:
            o = _norm_op(op)
            if o is not None:
                norm.append(o)
        self._ops: tuple[tuple, ...] = tuple(norm)
        self._aux: list[Any] = [None] * len(self._ops)

    @classmethod
    def _trusted(cls, ops: Sequence[tuple]) -> Remap:
        r = cls.__new__(cls)
        r._ops = tuple(ops)
        r._aux = [None] * len(r._ops)
        return r

    @property
    def ops(self) -> tuple[tuple, ...]:
        return self._ops

    @property
    def identity(self) -> bool:
        return not self._ops

    def __repr__(self) -> str:
        kinds = ", ".join(op[0] for op in self._ops[:8])
        more = ", ..." if len(self._ops) > 8 else ""
        return f"Remap([{kinds}{more}])"

    def _gaps(self, i: int) -> array:
        """inss/unmove: gaps[j] = positions[j] - j (old position before which the j-th new item lands)."""
        aux = self._aux[i]
        if aux is None:
            op = self._ops[i]
            p = op[1] if op[0] == "inss" else op[2]
            aux = array("Q", map(sub, p, range(len(p))))
            self._aux[i] = aux
        return aux

    def _sentinel(self, i: int) -> array:
        """dels/move: the positions with a U64_MAX sentinel appended (vectorized bisect lookups)."""
        aux = self._aux[i]
        if aux is None:
            aux = array("Q", self._ops[i][1])
            aux.append(U64_MAX)
            self._aux[i] = aux
        return aux

    def delta(self) -> int:
        """Count after minus count before."""
        d = 0
        for op in self._ops:
            kind = op[0]
            if kind == "ins":
                d += op[2]
            elif kind == "del":
                d -= op[2]
            elif kind == "inss":
                d += len(op[1])
            elif kind == "dels":
                d -= len(op[1])
            elif kind == "pieces":
                d += op[2].count - op[1].count
        return d

    def map(self, pos: int) -> int | None:
        """New position of old position ``pos``; ``None`` when the item was deleted."""
        p = _nonneg(pos, "position")
        for i, op in enumerate(self._ops):
            kind = op[0]
            if kind == "ins":
                if p >= op[1]:
                    p += op[2]
            elif kind == "del":
                s = op[1]
                if p >= s:
                    if p < s + op[2]:
                        return None
                    p -= op[2]
            elif kind == "inss":
                p += bisect.bisect_right(self._gaps(i), p)
            elif kind == "dels":
                d = op[1]
                r = bisect.bisect_left(d, p)
                if r < len(d) and d[r] == p:
                    return None
                p -= r
            elif kind == "move":
                mp, to = op[1], op[2]
                r = bisect.bisect_left(mp, p)
                if r < len(mp) and mp[r] == p:
                    p = to + r
                else:
                    q = p - r
                    p = q + len(mp) if q >= to else q
            elif kind == "unmove":
                to, mp = op[1], op[2]
                k = len(mp)
                if to <= p < to + k:
                    p = mp[p - to]
                else:
                    q = p if p < to else p - k
                    p = q + bisect.bisect_right(self._gaps(i), q)
            else:  # pieces
                old, new = op[1], op[2]
                if p >= old.count:
                    raise IndexError(f"position {p} out of range (count {old.count})")
                r2 = new.pos_of(old.rid_at(p))
                if r2 is None:
                    return None
                p = r2
        return p

    def map_array(self, positions: Any) -> array:
        """Map many positions: order kept, deleted dropped. An ascending input is merged per op in
        O(n + k); other input uses C-level bisects."""
        x = _u64(positions, "positions")
        if not self._ops or not x:
            return array("Q", x)
        asc = _nondecreasing(x)
        for i, op in enumerate(self._ops):
            kind = op[0]
            if kind == "ins":
                x, asc = _map_ins(x, asc, op[1], op[2])
            elif kind == "del":
                x, asc = _map_del(x, asc, op[1], op[2])
            elif kind == "inss":
                x, asc = _map_inss(x, asc, self._gaps(i))
            elif kind == "dels":
                x, asc = _map_dels(x, asc, op[1], self._sentinel(i))
            elif kind == "move":
                x = _map_move(x, op[1], self._sentinel(i), op[2])
                asc = _nondecreasing(x)
            elif kind == "unmove":
                x = _map_unmove(x, op[1], op[2], self._gaps(i))
                asc = _nondecreasing(x)
            else:  # pieces
                x = _map_pieces(x, op[1], op[2])
                asc = _nondecreasing(x)
            if not x:
                break
        return x if x is not positions else array("Q", x)

    def map_chunks(self, chunks: Iterable[Any], cancel: Any = None) -> Iterator[array]:
        """Streaming :meth:`map_array` over consecutive chunks (e.g. ``RecordSet.iter_chunks()``); empty
        results are skipped; ``cancel`` is checked per chunk."""
        for chunk in chunks:
            if cancel is not None:
                cancel.check()
            out = self.map_array(chunk)
            if out:
                yield out

    def inverse(self) -> Remap:
        """New position -> old position (new items map to ``None``); exact for every op."""
        inv = []
        for op in reversed(self._ops):
            kind = op[0]
            if kind == "ins":
                inv.append(("del", op[1], op[2]))
            elif kind == "del":
                inv.append(("ins", op[1], op[2]))
            elif kind == "inss":
                inv.append(("dels", op[1]))
            elif kind == "dels":
                inv.append(("inss", op[1]))
            elif kind == "move":
                inv.append(("unmove", op[2], op[1]))
            elif kind == "unmove":
                inv.append(("move", op[2], op[1]))
            else:
                inv.append(("pieces", op[2], op[1]))
        return Remap._trusted(inv)

    def then(self, other: Remap) -> Remap:
        """``self`` followed by ``other`` (old coordinates of self -> new coordinates of other)."""
        if not isinstance(other, Remap):
            raise TypeError("Remap.then() takes a Remap")
        return Remap._trusted(self._ops + other._ops)

    # -- persistence ----------------------------------------------------------------------
    def to_json(self, blob_writer: Callable[[bytes], Sequence[int]] | None = None) -> list:
        """JSON-ready list of ops. Arrays longer than :data:`BLOB_MIN` are written with
        ``blob_writer(u64 LE bytes) -> [vstart, vend]`` as ``{"blob": [vstart, vend]}`` when one is given
        (else inline lists); "pieces" tables are their flat ``lo, hi`` pair arrays."""

        def arr(a: array) -> Any:
            if blob_writer is not None and len(a) > BLOB_MIN:
                span = blob_writer(_le_bytes(a))
                return {"blob": [int(span[0]), int(span[1])]}
            return a.tolist()

        def table(t: PieceTable) -> Any:
            lo, hi = t._flat()
            return arr(_interleave(lo, hi))

        out: list = []
        for op in self._ops:
            kind = op[0]
            if kind in ("ins", "del"):
                out.append([kind, op[1], op[2]])
            elif kind in ("inss", "dels"):
                out.append([kind, arr(op[1])])
            elif kind == "move":
                out.append([kind, arr(op[1]), op[2]])
            elif kind == "unmove":
                out.append([kind, op[1], arr(op[2])])
            else:
                out.append([kind, table(op[1]), table(op[2])])
        return out

    @classmethod
    def from_json(cls, data: Any, blob_reader: Callable[[int, int], bytes] | None = None) -> Remap:
        """Inverse of :meth:`to_json`; ``blob_reader(vstart, vend) -> bytes`` resolves blobs. Invalid data
        raises ``ValueError``."""
        if not isinstance(data, list):
            raise ValueError("remap JSON must be a list")

        def scalar(v: Any) -> int:
            if type(v) is not int or v < 0:
                raise ValueError(f"remap JSON: expected a non-negative integer, got {v!r}")
            return v

        def arr(v: Any) -> array:
            if isinstance(v, dict):
                blob = v.get("blob")
                if not isinstance(blob, list) or len(blob) != 2 or len(v) != 1:
                    raise ValueError("remap JSON: invalid blob reference")
                vs, ve = scalar(blob[0]), scalar(blob[1])
                if blob_reader is None:
                    raise ValueError("remap JSON references a blob but no blob_reader was given")
                return _from_le(blob_reader(vs, ve), "remap blob")
            if isinstance(v, list):
                if not all(type(e) is int for e in v):
                    raise ValueError("remap JSON: arrays hold integers only")
                return _copy_u64(v, "remap JSON array")
            raise ValueError(f"remap JSON: expected an array, got {type(v).__name__}")

        def table(v: Any) -> PieceTable:
            pairs = arr(v)
            if len(pairs) % 2:
                raise ValueError("remap JSON: piece table with an odd number of values")
            return PieceTable.from_bytes(_le_bytes(pairs))

        ops: list[tuple] = []
        for item in data:
            if not isinstance(item, list) or not item or item[0] not in _OP_KINDS:
                raise ValueError(f"remap JSON: invalid op {item!r}"[:200])
            kind = item[0]
            try:
                if kind in ("ins", "del"):
                    _k, pos, n = item
                    ops.append((kind, scalar(pos), scalar(n)))
                elif kind in ("inss", "dels"):
                    _k, a = item
                    ops.append((kind, arr(a)))
                elif kind == "move":
                    _k, a, to = item
                    ops.append((kind, arr(a), scalar(to)))
                elif kind == "unmove":
                    _k, to, a = item
                    ops.append((kind, scalar(to), arr(a)))
                else:
                    _k, old, new = item
                    ops.append((kind, table(old), table(new)))
            except (TypeError, ValueError) as exc:
                if isinstance(exc, ValueError) and str(exc).startswith("remap JSON"):
                    raise
                raise ValueError(f"remap JSON: invalid {kind!r} op") from exc
        return cls(ops)


# ----------------------------------------------------------------------------------
# NodeChange / ChangeSet
# ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class NodeChange:
    """The children of one container changed structure.

    A ChangeSet lists NodeChanges ancestors first; holders apply the record remap, then the NodeChanges in
    list order (each ipath is valid after the previous entries)."""

    record: int | None          # record position AFTER the step; None = wrapper
    ipath: tuple[int, ...]      # child-index path of the container AFTER the step (() = root)
    remap: Remap                # old child index -> new child index
    count: int                  # child count after the step

    def __post_init__(self) -> None:
        if not isinstance(self.remap, Remap):
            raise TypeError("NodeChange.remap must be a Remap")
        object.__setattr__(self, "ipath", tuple(operator.index(i) for i in self.ipath))
        if self.record is not None:
            _nonneg(self.record, "record")
        _nonneg(self.count, "count")


def invert_node_changes(nodes: Sequence[NodeChange], record_remap: Remap | None = None) -> tuple[NodeChange, ...]:
    """Inverse NodeChange list of a step: reversed, inverse remaps, same ipaths, ``count - remap.delta()``.

    ``record`` (a position after the step) is mapped back with ``record_remap.inverse()`` when the step also
    moved records; NodeChanges of records the step inserted (no position before it) are dropped."""
    inv = record_remap.inverse() if record_remap is not None and not record_remap.identity else None
    out = []
    for nc in reversed(nodes):
        rec = nc.record
        if rec is not None and inv is not None:
            rec = inv.map(rec)
            if rec is None:
                continue
        out.append(NodeChange(rec, nc.ipath, nc.remap.inverse(), nc.count - nc.remap.delta()))
    return tuple(out)


# ----------------------------------------------------------------------------------
# generations
# ----------------------------------------------------------------------------------
_GEN_LOCK = threading.Lock()
_GEN_COUNTER = itertools.count(1)
_GEN_LAST = 0
_GEN_LIVE: weakref.WeakValueDictionary[int, OverlayState] = weakref.WeakValueDictionary()


def next_gen() -> int:
    """Process-unique, increasing generation number (thread-safe; never 0)."""
    global _GEN_LAST
    with _GEN_LOCK:
        g = next(_GEN_COUNTER)
        _GEN_LAST = g
    return g


def live_states() -> list[OverlayState]:
    """Every :class:`OverlayState` of this process that is still referenced somewhere (a snapshot)."""
    with _GEN_LOCK:
        return list(_GEN_LIVE.values())


def _claim_gen(state: OverlayState, gen: int) -> int:
    global _GEN_LAST
    with _GEN_LOCK:
        if gen <= 0:
            gen = next(_GEN_COUNTER)
            _GEN_LAST = gen
        elif gen > _GEN_LAST:
            raise ValueError(f"gen {gen} was not issued by next_gen()")
        elif _GEN_LIVE.get(gen) is not None:
            raise ValueError(f"gen {gen} is already used by another state (derive states with derive())")
        _GEN_LIVE[gen] = state
    return gen


# ----------------------------------------------------------------------------------
# OverlayState
# ----------------------------------------------------------------------------------
def _overrides(layers: Sequence[Any], lo: int, hi: int) -> Any:
    """Sorted rids in ``[lo, hi)`` that any layer overrides."""
    found = [k for k in (layer.keys_in(lo, hi) for layer in layers) if k]
    if not found:
        return _EMPTY
    if len(found) == 1:
        return found[0]
    return array("Q", sorted(set(found[0]).union(*found[1:])))


def _classify_segment(p: int, r: int, n: int, base: int, ov: Any) -> list[tuple[int, int, int, bool]]:
    e = r + n
    bend = e if e < base else base
    cur = r
    parts: list[list[Any]] = []   # [rid_start, rid_end, pure]
    if bend > r and ov:
        a = bisect.bisect_left(ov, r)
        b = bisect.bisect_left(ov, bend, a)
        if b > a:
            sov = ov[a:b]
            k = b - a
            if sov[-1] - sov[0] + 1 == k:
                blocks: Iterable[tuple[int, int]] = ((sov[0], sov[-1] + 1),)
            else:
                brk = list(compress(range(1, k), map(ne, islice(sov, 1, None), map(add, sov, repeat(1)))))
                blocks = [(sov[x], sov[y - 1] + 1) for x, y in zip([0, *brk], [*brk, k])]
            for x, y in blocks:
                if x > cur:
                    parts.append([cur, x, True])
                parts.append([x, y, False])
                cur = y
    if cur < bend:
        parts.append([cur, bend, True])
        cur = bend
    if cur < e:
        if parts and not parts[-1][2] and parts[-1][1] == cur:
            parts[-1][1] = e
        else:
            parts.append([cur, e, False])
    return [(p + x - r, x, y - x, pure) for x, y, pure in parts]


def _classify_window(ps: array, ls: array, ns: array, base: int, layers: Sequence[Any]
                     ) -> Iterator[tuple[int, int, int, bool]]:
    m = len(ls)
    gap = CLUSTER_GAP
    i = 0
    while i < m:
        clo = ls[i]
        chi = clo + ns[i]
        j = i + 1
        while j < m:
            r = ls[j]
            e = r + ns[j]
            if r > chi + gap or clo > e + gap:
                break
            if r < clo:
                clo = r
            if e > chi:
                chi = e
            j += 1
        top = chi if chi < base else base
        ov = _overrides(layers, clo, top) if clo < top else _EMPTY
        for t in range(i, j):
            yield from _classify_segment(ps[t], ls[t], ns[t], base, ov)
        i = j


def _opt_span(value: Any, what: str) -> tuple[int, int] | None:
    if value is None:
        return None
    try:
        return _span(value)
    except ValueError as exc:
        raise ValueError(f"{what}: {exc}") from exc


@dataclass(frozen=True, eq=False)
class OverlayState:
    """One immutable edit state (§12.1). Construct with :meth:`initial`, derive with :meth:`derive` /
    :meth:`with_content`; every constructed state gets a fresh, process-unique ``gen``."""

    base_count: int
    pieces: PieceTable
    layers: tuple
    wrapper: tuple[int, int] | None = None        # wrapped docs: virtual span of the edited wrapper text W'
    wrapper_hole: tuple[int, int] | None = None   # virtual span of the record-container placeholder in W'
    flags: int = 0                                # FLAG_* bits of all edited content (OR'ed into doc.flags)
    next_rid: int = 0                             # raised to base_count when lower
    version: int = 0                              # persisted head seq (0 = no step)
    gen: int = 0                                  # 0 -> a fresh next_gen() is assigned
    reader: DatReader | None = None

    def __post_init__(self) -> None:
        base = _nonneg(self.base_count, "base_count")
        if not isinstance(self.pieces, PieceTable):
            raise TypeError("OverlayState.pieces must be a PieceTable")
        layers = tuple(self.layers)
        if not layers or not isinstance(layers[-1], DictLayer):
            raise ValueError("OverlayState.layers must end with a DictLayer")
        for layer in layers:
            if not isinstance(layer, (DictLayer, ArrayLayer)):
                raise TypeError(f"invalid content layer: {layer!r}")
        object.__setattr__(self, "layers", layers)
        wrapper = _opt_span(self.wrapper, "wrapper")
        hole = _opt_span(self.wrapper_hole, "wrapper_hole")
        if hole is not None and (wrapper is None or not wrapper[0] <= hole[0] <= hole[1] <= wrapper[1]):
            raise ValueError("wrapper_hole must lie inside wrapper")
        object.__setattr__(self, "wrapper", wrapper)
        object.__setattr__(self, "wrapper_hole", hole)
        _nonneg(self.flags, "flags")
        _nonneg(self.version, "version")
        if _nonneg(self.next_rid, "next_rid") < base:
            object.__setattr__(self, "next_rid", base)
        if self.reader is not None and not isinstance(self.reader, DatReader):
            raise TypeError("OverlayState.reader must be a DatReader or None")
        object.__setattr__(self, "gen", _claim_gen(self, operator.index(self.gen)))

    # -- factories ------------------------------------------------------------------------
    @classmethod
    def initial(cls, base_count: int, reader: DatReader | None) -> OverlayState:
        """The unedited state: identity pieces, one empty DictLayer, ``next_rid = base_count``."""
        base = _nonneg(base_count, "base_count")
        return cls(base_count=base, pieces=PieceTable.identity(base), layers=(DictLayer(),),
                   next_rid=base, reader=reader)

    def derive(self, **changes: Any) -> OverlayState:
        """A new state (fresh gen) with ``changes`` applied to the fields."""
        if "gen" in changes:
            raise TypeError("derive() assigns the gen itself")
        return dataclasses.replace(self, gen=0, **changes)

    def with_content(self, changes: Mapping[int, tuple[int, int] | None], **more: Any) -> OverlayState:
        """A new state whose top DictLayer has ``changes`` applied (``None`` removes), plus field changes."""
        layers = self.layers[:-1] + (self.layers[-1].with_changes(changes),)
        return self.derive(layers=layers, **more)

    # -- positions -------------------------------------------------------------------------
    @property
    def count(self) -> int:
        return self.pieces.count

    @property
    def edited(self) -> bool:
        """``version > 0`` (dirty; also true after a restore)."""
        return self.version > 0

    @property
    def top(self) -> DictLayer:
        """The top (newest) layer, always a DictLayer."""
        return self.layers[-1]

    def rid_at(self, pos: int) -> int:
        return self.pieces.rid_at(pos)

    def rids(self, pos: int, n: int) -> array:
        return self.pieces.rids(pos, n)

    def rids_at(self, positions: Any) -> array:
        return self.pieces.rids_at(positions)

    # -- content ---------------------------------------------------------------------------
    def content(self, rid: int) -> tuple[int, int] | None:
        """Span of the newest layer that has ``rid``; ``None`` = base content."""
        for layer in reversed(self.layers):
            span = layer.get(rid)
            if span is not None:
                return span
        return None

    def is_pure(self, rid: int) -> bool:
        """A base rid that no layer overrides (its raw file bytes are its content)."""
        return rid < self.base_count and self.content(rid) is None

    def runs(self, pos0: int = 0, pos1: int | None = None) -> Iterator[tuple[int, int, int, bool]]:
        """``(pos, rid_lo, n, pure)`` with ``n <= WINDOW``: maximal runs (within windows of ``WINDOW``
        positions) of consecutive rids that are all PURE (base rids no layer overrides) or all IMPURE
        (overridden or new rids). One ``keys_in`` per layer per window (per rid cluster)."""
        count = self.count
        pos0 = _nonneg(pos0, "pos0")
        pos1 = count if pos1 is None else min(count, operator.index(pos1))
        layers = [layer for layer in self.layers if len(layer)]
        base = self.base_count
        pieces = self.pieces
        w = pos0
        while w < pos1:
            w1 = min(pos1, w + WINDOW)
            ps, ls, ns = pieces._segments(w, w1)
            if layers:
                yield from _classify_window(ps, ls, ns, base, layers)
            else:
                for p, r, n in zip(ps, ls, ns):
                    if r + n <= base:
                        yield (p, r, n, True)
                    elif r >= base:
                        yield (p, r, n, False)
                    else:
                        k = base - r
                        yield (p, r, k, True)
                        yield (p + k, base, n - k, False)
            w = w1

    def contents(self, rid_lo: int, n: int) -> array:
        """Flat ``(start, end)`` ``array('Q')`` of the rids ``rid_lo .. rid_lo+n-1`` of an IMPURE run."""
        rid_lo, n = _nonneg(rid_lo, "rid_lo"), _nonneg(n, "n")
        if not n:
            return array("Q")
        hi = rid_lo + n
        out: array | None = None
        filled = bytearray()
        missing = n
        for layer in reversed(self.layers):
            if not len(layer):
                continue
            rids, pairs = layer.spans_in(rid_lo, hi)
            if not rids:
                continue
            if out is None:
                if len(rids) == n:
                    return pairs
                out = array("Q", bytes(16 * n))
                filled = bytearray(n)
            for idx, rid in enumerate(rids):
                j = rid - rid_lo
                if not filled[j]:
                    filled[j] = 1
                    out[2 * j] = pairs[2 * idx]
                    out[2 * j + 1] = pairs[2 * idx + 1]
                    missing -= 1
            if not missing:
                return out
        first = rid_lo if out is None else rid_lo + filled.index(0)
        if first >= self.base_count:
            raise _layer_corrupt("", f"record id {first} has no content")
        raise ValueError(f"rid {first} is not overridden (pure); contents() takes impure runs only")

    def read(self, vstart: int, vend: int) -> bytes:
        """Bytes of a virtual span in the segment space (through :attr:`reader`)."""
        if self.reader is None:
            raise _layer_corrupt("", f"no segment reader for span ({vstart}, {vend})")
        return self.reader.read(vstart, vend)


# ----------------------------------------------------------------------------------
# ChangeSet
# ----------------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class ChangeSet:
    """What one applied step changed (§12.1); ``records`` above :data:`RECORDS_MAX` becomes ``None``."""

    state: OverlayState
    kind: str            # "edit" | "undo" | "redo" | "bulk" | "patch" | "restore" | "discard"
    label: str           # localized step label; "" for restore/discard
    records: frozenset[int] | None    # positions (after) whose CONTENT changed; None = potentially every record
    remap: Remap | None               # record positions moved; None = none (an identity Remap becomes None)
    nodes: tuple[NodeChange, ...] = ()
    wrapper: bool = False
    reset: bool = False
    select: tuple | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, OverlayState):
            raise TypeError("ChangeSet.state must be an OverlayState")
        if self.kind not in CHANGE_KINDS:
            raise ValueError(f"invalid ChangeSet kind {self.kind!r}")
        records = self.records
        if records is not None:
            if hasattr(records, "__len__") and len(records) > 2 * RECORDS_MAX:
                records = None                     # many (no need to build a huge set first)
            elif not isinstance(records, frozenset):
                records = frozenset(records)
            if records is not None and len(records) > RECORDS_MAX:
                records = None
            object.__setattr__(self, "records", records)
        remap = self.remap
        if remap is not None:
            if not isinstance(remap, Remap):
                raise TypeError("ChangeSet.remap must be a Remap or None")
            if remap.identity:
                object.__setattr__(self, "remap", None)
        nodes = tuple(self.nodes)
        for nc in nodes:
            if not isinstance(nc, NodeChange):
                raise TypeError("ChangeSet.nodes holds NodeChange objects")
        object.__setattr__(self, "nodes", nodes)

    @property
    def gen(self) -> int:
        return self.state.gen
