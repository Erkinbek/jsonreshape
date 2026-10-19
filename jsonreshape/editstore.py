"""The edit layer on disk (docs/ARCHITECTURE.md §12.2): SQLite steps + append-only segments, recovery, locks.

One *layer* holds the unsaved changes of one version of one document (the key is
:func:`layer_key`: the file identity, the format kind, the record path and the base record count).
It lives in ``appdirs.edits_dir()/<key>/`` or, with ``Settings.edits_location == "beside"``, in
``<file>.jredit/`` next to the file. Files of a layer:

* ``layer.sqlite`` (+ ``-wal`` while open): tables ``meta``, ``steps``, ``checkpoint`` exactly as §12.2,
  opened with ``locking_mode=EXCLUSIVE``, ``journal_mode=WAL``, ``synchronous=FULL``;
* ``seg-00000.dat`` (point edits and every blob: remap arrays, long step lists, piece-table snapshots,
  checkpoint layers) and ``seg-<n>.dat`` (one per bulk preview / big patch / big duplicate);
* ``layer-<n>.bin``: array layers (:func:`jsonreshape.overlay.write_array_layer`).

The lock of a layer is ``appdirs.edits_locks_dir()/<key>.lock`` (:class:`LayerLock`), never inside a
``.jredit`` folder, so taking it never creates anything next to the user's file.

Durability protocol (the continuous autosave): units are appended to a segment (unbuffered, at the
store's write position); :meth:`EditStore.commit` fsyncs the segments the step depends on, publishes
their ends to the :class:`~jsonreshape.overlay.DatReader` and then commits the step row and the meta
(``head``, ``next_rid``, ``ends``, ``files``) in ONE SQLite transaction. A crash at any point leaves
either the old or the new head; bytes appended after the last committed end are cut off at the next
open. ``set_head`` (undo/redo) is a durable transaction before it returns.

Decisions taken where §12.2 leaves room (documented in the stage-3 report):

* **Step.struct** is the list of *forward Remap ops* (the canonical ``Remap(...).ops`` form, e.g.
  ``[("dels", positions)]``). The rids of inserted items are ``Step.new_rids`` (``(pos, rid_lo, n)``
  entries exactly as :meth:`PieceTable.apply` consumes them) and the rids of removed items are
  ``Step.old_rids`` (the ``new_rids`` of ``remap.inverse()``, i.e. per removing op in REVERSE op order).
  ``old_rids`` is filled from the pre-step state by :meth:`EditStore.commit` (``state=`` or the store's
  own head table) and by :func:`apply_step` when missing; :func:`undo_step` needs it. For convenience a
  ``Remap`` object or ``[Remap, new_rids(, old_rids)]`` are accepted as ``struct`` too.
* Step fields added to §12.2 (all optional): ``new_rids``, ``old_rids``, ``next_rid`` (explicit
  high-water mark; it is also derived from the new rids, the content rids and "pieces" tables). Every
  field after ``label_key`` has a default; ``flags`` defaults to ``None`` = "unchanged" (a default of
  ``(0, 0)`` would silently clear the flags of an edited state). A segment the step's spans or array
  layer use that the store allocated (``new_segment``) and no committed step owns is adopted into
  ``Step.segments`` by ``commit`` (it is then deleted with the step when it is truncated as redo).
* ``content`` changes apply to the top DictLayer BEFORE ``push_layer`` pushes the array layer (and the
  new empty DictLayer); undo pops first and then reverts the content. ``apply_step`` / ``undo_step``
  check that the step fits the state (old top-layer entries, wrapper spans, the pushed layer) and raise
  :class:`LayerDamagedError` otherwise (a damaged layer, or a bug in the caller).
* ``pieces_blob`` is the piece table AFTER the step. The store writes it on every 64th structural step
  (counted since the last snapshot or the checkpoint) when it can compute the table (``commit(state=)``
  or its own head table); ``load_state`` takes the piece table from the nearest snapshot and replays
  only the structural ops after it.
* Every step row records ``ends``: the published ends of the segments it depends on (seg 0, its own
  segments, the segments of its array layer and of every span it references). ``load_state`` checks
  them against the segment files and every span of the step against them; the first failing step
  raises :class:`LayerDamagedError` (``editstore.error.damaged``) with ``.seq`` and ``.last_good``.
  Meta keys added to §12.2: ``key``, ``source_norm``, ``data_size``, ``ends`` (published end per
  segment over all committed data), ``files`` (private segments/layer files -> owning step, 0 = the
  checkpoint). At open (lock held, not read-only) unreferenced segment/layer/temporary files are
  deleted and segment tails beyond the committed ends are truncated.
* Redo truncation (a commit after undo) deletes the rows ``> head`` in the transaction; their private
  files -- like every file of a discarded, saved or head-0 layer -- are deleted only when no alive state
  of this process can read them any more (deferred deletion, see the section "file names never reused
  ..."): a pinned export, search or preview of an older state keeps working. Then the segments are
  forgotten in every reader of the folder and the array layers closed, so no handle keeps the disk space
  of a deleted file. Leftovers of a process that ended first are collected at the next open.
* Segment and layer-file NUMBERS are never reused within a process for one folder: a new layer in a folder
  this process used before (a discard, then a new edit) continues the numbers, so its point-edit segment
  may be ``seg-<n>.dat`` with ``n > 0`` (meta key ``point_seg``, default 0; :attr:`EditStore.point_segment`;
  ``append(seg=None)`` writes there). A reader of an old state can never open the new layer's file by the
  old name, and virtual offsets stay unique (§12.0).
* Checksums: every step row records ``crc`` = ``[seg, start, end, crc32]`` of the bytes it appended to each
  segment (computed while appending, no read back); the checkpoint row holds those of the folded steps,
  merged per segment (``crc32_combine``; bytes of steps truncated as redo are read once and included).
  ``load_state`` (restore, export of a pending layer) verifies them: a mismatch raises
  :class:`LayerDamagedError` at that step (the checkpoint: ``last_good=None``). Rows written before
  checksums existed have none. A range of 64 MiB or more (the segment of a bulk step) is read and
  checksummed in pieces by up to 4 threads at once, joined with ``crc32_combine`` (stage-6 performance
  work P6: the restore of a 2 GB bulk layer was bound by reading it in one thread; the check itself is
  unchanged and still finishes before the state is returned).
* :meth:`LayerLock.for_doc` also locks the PHYSICAL file (:func:`file_lock_key`: volume serial + file id),
  so one file opened under two spellings (junction, ``subst``, mapped drive / UNC) cannot be edited twice.
* ``maybe_trim(limit)``: while ``head - checkpoint > limit + 64`` the oldest 64 steps are replayed
  forward from the in-memory checkpoint state and folded into a new checkpoint (dict layers and the
  piece table as blobs in seg 0, unchanged ones reuse their previous blob) in one transaction.
* Lazy creation: :meth:`EditStore.open` with no layer returns a store whose folder, database and
  segments are created by the first write (``commit``, ``append``, ``new_segment``,
  ``new_layer_path``, ``journal``). For ``"beside"`` the folder is tried first; when it cannot be
  created the layer goes to the app folder with the notice ``editstore.warn.beside_fallback``
  (:attr:`EditStore.warnings`, :func:`take_notices`). The public :func:`layer_dir` decides the same
  way with a probe file (it never leaves a ``.jredit`` folder behind).
* Where an existing layer is looked for: a MATCHING layer in either location wins (the one of the
  configured location first); otherwise a new layer is planned in the configured location. A
  ``.jredit`` folder holding a layer of an older version of the file (stale) blocks creating a new
  one there: ``EditError("editstore.error.stale")`` until it is exported/deleted (``allow_stale`` opens
  it; a stale store is always read-only, :attr:`EditStore.stale`). ``mark_discarded``/``discard`` are
  allowed on read-only stores (deleting a pending/stale layer after its export). A folder given with
  ``dir=`` is classified as "appdata"/"beside" when it is one of the two standard places, else
  "custom" (custom layers -- the CLI's temporary ones -- are not registered in ``index.json``).
  :func:`delete_layer` removes a layer that cannot even be opened (damaged database) under its lock.
* Locks: :class:`LayerLock` uses ``msvcrt.locking`` (POSIX: ``flock``) on the lock file plus a
  process-wide registry; ``acquire()`` retries for 2 s and waits for a release when THIS process holds
  the key. A held lock is released by ``release()``, when the LayerLock object is garbage-collected
  and by the OS when the process dies. A key held elsewhere raises ``EditError("edits.error.locked")``
  (the existing key with the user-facing text; no ``editstore.error.locked`` duplicate). An unusable
  lock folder raises ``EditError("editstore.error.unavailable")`` -- also from :func:`find_pending`.
* :func:`find_pending` reads each candidate layer only while it holds that layer's lock (non-blocking
  probe); a locked layer is reported with the information of ``index.json`` (``locked=True``).
  Head-0 layers and tombstoned (``state="discarded"``) ones are purged silently when their lock is
  free; layers with an interrupted save journal are reported even at head 0. ``damaged`` is a light
  check (meta, segment sizes, layer files, step rows); a full check happens in ``load_state``.
* ``edits_dir()/index.json`` (written under ``query.file_lock``, best effort) registers every layer of
  the two standard locations: key, folder, normalized source path, creation time and -- updated at
  close -- the step count and the last label. It lets :func:`find_pending` find stale layers of the
  same source path and describe locked ones.
* The store reads its own blobs through a private DatReader, so a Document closing the public
  :attr:`EditStore.reader` (it owns it once it shows a state) never breaks the store.
  ``close()`` never closes the public reader or array layers handed out (``close(close_reader=True)``
  for owners such as an export of a pending layer).

Signatures beyond §12.2 (keyword extensions, all optional): ``commit(step, *, state=None)``,
``load_state(*, upto=None, progress=None, cancel=None)`` (``upto`` loads an older state, e.g.
``LayerDamagedError.last_good``; progress counts steps), ``close(*, close_reader=False)``,
``find_pending(doc, *, settings=None, lock=None)`` (``lock``: the caller's held LayerLock counts as
free), ``LayerLock(key)`` / ``LayerLock.for_doc(doc)`` with ``acquire(timeout=None)``,
``try_acquire()``, ``release()``, ``held``, ``is_locked(key)`` (static probe). Further public helpers:
:class:`LayerDamagedError`, :class:`StepInfo`, :func:`beside_dir`, :func:`delete_layer`,
:func:`removed_rids`, :func:`step_label`, :func:`step_next_rid`, :func:`take_notices`,
:func:`notice_texts`, :func:`file_lock_key`, :func:`crc32_combine`, :func:`retained_files`,
:func:`layer_sources` (the sources ``index.json`` lists, read once and cheaply: who may have unsaved edits); on
the store:
``layer(name)`` (cached ArrayLayer of the folder), ``drop_layer_file(path)``, ``published(seg)``,
``segment_path(seg)``, ``point_segment``, ``checkpoint_seq`` (undo floor), ``max_seq``, ``saving`` (journal),
``warnings``, ``location``, ``created``, ``stale``.

Thread safety: every public method of :class:`EditStore` runs under the store's lock (the SQLite
connection is created with ``check_same_thread=False``). Bulk previews may append to their own
segment from another thread while point edits are committed.
"""

from __future__ import annotations

import atexit
import errno as _errno
import hashlib
import json
import logging
import math
import operator
import os
import queue
import re
import secrets
import sqlite3
import sys
import threading
import time
import weakref
import zlib
from array import array
from dataclasses import dataclass, field
from itertools import compress, islice, repeat
from operator import add, ne
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Sequence

import orjson

from . import __version__
from .appdirs import edits_dir, edits_locks_dir
from .errors import EditError, JsonReshapeError
from .fileio import (FileIdentity, atomic_write, long_path, make_dirs, normalize_path, open_shared, os_error,
                     read_limited)
from .i18n import has, t
from .overlay import (
    BLOB_MIN,
    EDIT_BASE,
    MAX_SEGMENT,
    SEG_SIZE_LIMIT,
    ArrayLayer,
    DatReader,
    DictLayer,
    NodeChange,
    OverlayState,
    PieceTable,
    Remap,
    array_layers_of,
    edit_offset,
    live_states,
    readers_of,
    segment_name,
    set_reader_closed_hook,
    split_edit_offset,
)
from .settings import Settings, get_settings
from .tasks import CancelToken, Progress, check

log = logging.getLogger(__name__)

__all__ = [
    "BESIDE_SUFFIX",
    "DB_NAME",
    "FORMAT_VERSION",
    "LOCK_RETRY",
    "PIECES_BLOB_EVERY",
    "TRIM_BATCH",
    "EditStore",
    "LayerDamagedError",
    "LayerLock",
    "PendingInfo",
    "Step",
    "StepInfo",
    "apply_step",
    "beside_dir",
    "crc32_combine",
    "delete_layer",
    "file_lock_key",
    "find_pending",
    "layer_dir",
    "layer_key",
    "layer_sources",
    "lock_path",
    "notice_texts",
    "removed_rids",
    "retained_files",
    "step_label",
    "step_next_rid",
    "take_notices",
    "undo_step",
]

# ----------------------------------------------------------------------------------
# constants
# ----------------------------------------------------------------------------------
FORMAT_VERSION = 1
DB_NAME = "layer.sqlite"
BESIDE_SUFFIX = ".jredit"
INDEX_NAME = "index.json"
INDEX_VERSION = 1
LOCK_RETRY = 2.0                 # LayerLock.acquire(): seconds a busy key is retried
PIECES_BLOB_EVERY = 64           # every 64th structural step stores a full piece-table snapshot
TRIM_BATCH = 64                  # maybe_trim(): steps folded into the checkpoint at once
LIST_BLOB_MIN = BLOB_MIN         # step lists longer than this are stored as segment blobs
INDEX_LOCK_TIMEOUT = 5.0         # seconds to wait for index.json's file lock
_INDEX_MAX_BYTES = 16 << 20
_DB_TIMEOUT = 0.5                # busy timeout of the store's own connection (the layer lock is held)
_U64_MAX = (1 << 64) - 1
_I64_MIN = -(1 << 63)
_KEY_RE = re.compile(r"[0-9a-f]{32}")
_SEG_FILE = re.compile(r"seg-(\d{5,})\.dat")
_LAYER_FILE = re.compile(r"layer-(\d{5,})\.bin")
_TEMP_FILE = re.compile(r"(jrlayer-.*|.*\.tmp|.*\.part)")
_DB_FILES = (DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm", DB_NAME + "-journal")
_WINDOWS = sys.platform == "win32"
_BIG_ENDIAN = sys.byteorder == "big"
_SQLITE_BUSY = (5, 6)            # SQLITE_BUSY, SQLITE_LOCKED (primary codes)
_SQLITE_FULL = 13

_META_INT = ("format_version", "base_count", "head", "next_rid", "data_size", "point_seg")
_META_FLOAT = ("created", "updated")
_META_JSON = ("identity", "record_path", "ends", "files")
_META_REQUIRED = ("format_version", "state", "head", "base_count", "identity", "kind", "created", "updated",
                  "next_rid")


def _crash_point(name: str) -> None:
    """No-op hook at the durability points (tests replace it in a subprocess to simulate a crash)."""
    return None


def _raw_write(f: Any, view: memoryview) -> int:
    """One write of an unbuffered segment handle (tests replace it to simulate a full disk)."""
    return f.write(view)


# ----------------------------------------------------------------------------------
# errors
# ----------------------------------------------------------------------------------
class LayerDamagedError(EditError):
    """The edit layer cannot be loaded (``editstore.error.damaged``).

    ``seq`` is the first step that failed, ``last_good`` the last step whose state can still be loaded
    (``load_state(upto=last_good)``) or ``None`` when even the checkpoint is damaged; ``detail`` says why
    (English, for the log)."""

    key = "editstore.error.damaged"

    def __init__(self, key: str | None = None, /, *, seq: int = 0, last_good: int | None = None,
                 detail: str = "", **params: Any) -> None:
        super().__init__(key, seq=seq, last_good=last_good, detail=detail, **params)
        self.seq = seq
        self.last_good = last_good
        self.detail = detail


def _damaged(seq: int, detail: str, *, last_good: int | None | str = "auto") -> LayerDamagedError:
    lg = (seq - 1 if seq > 0 else None) if last_good == "auto" else last_good
    log.warning("edit layer damaged at step %s: %s", seq, detail)
    return LayerDamagedError(seq=seq, last_good=lg, detail=detail)  # type: ignore[arg-type]


def _is_disk_full(exc: BaseException) -> bool:
    if isinstance(exc, OSError):
        return exc.errno == _errno.ENOSPC or getattr(exc, "winerror", None) in (39, 112)
    if isinstance(exc, sqlite3.Error):
        code = getattr(exc, "sqlite_errorcode", None)
        return (code is not None and code & 0xFF == _SQLITE_FULL) or "full" in str(exc).lower()
    return False


def _is_busy(exc: sqlite3.Error) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None and code & 0xFF in _SQLITE_BUSY:
        return True
    return "locked" in str(exc).lower() or "busy" in str(exc).lower()


def _write_error(exc: BaseException, path: Path | str) -> EditError:
    """A localized EditError for a failed write of the layer (disk full is told apart)."""
    if isinstance(exc, EditError):
        return exc
    if _is_disk_full(exc):
        return EditError("editstore.error.disk_full", path=str(path))
    if isinstance(exc, JsonReshapeError):
        detail = exc.message
    elif isinstance(exc, OSError):
        detail = os_error(exc, path, writing=True).message
    else:
        detail = str(exc)
    return EditError("editstore.error.write", path=str(path), detail=detail)


def _unavailable(exc: BaseException) -> EditError:
    detail = exc.message if isinstance(exc, JsonReshapeError) else str(exc)
    return EditError("editstore.error.unavailable", detail=detail)


# ----------------------------------------------------------------------------------
# JSON helpers (orjson; exact round trip)
# ----------------------------------------------------------------------------------
def _dumps(obj: Any) -> str:
    """Compact JSON text. Strings with lone surrogates (decoded JSON keys may hold them) are not
    representable by orjson: the standard library escapes them instead (``\\udXXX``)."""
    try:
        return orjson.dumps(obj).decode("utf-8")
    except TypeError:
        return json.dumps(obj, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _loads(text: str | bytes | bytearray | memoryview) -> Any:
    try:
        return orjson.loads(text)
    except orjson.JSONDecodeError:
        if isinstance(text, memoryview):
            text = bytes(text)
        return json.loads(text)  # escaped lone surrogates written by the fallback of _dumps


def _json_value(value: Any, what: str, *, freeze: bool = False, depth: int = 0) -> Any:
    """``value`` checked to round-trip exactly through JSON (ints within 64 bits, finite floats, str
    keys); tuples become lists (``freeze``: lists become tuples)."""
    if depth > 64:
        raise ValueError(f"{what}: nested too deeply")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if not _I64_MIN <= value <= _U64_MAX:
            raise ValueError(f"{what}: integer out of the 64-bit range")
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{what}: non-finite number")
        return value
    if isinstance(value, (list, tuple)):
        items = [_json_value(v, what, freeze=freeze, depth=depth + 1) for v in value]
        return tuple(items) if freeze else items
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError(f"{what}: object keys must be strings")
            out[k] = _json_value(v, what, freeze=freeze, depth=depth + 1)
        return out
    raise ValueError(f"{what}: {type(value).__name__} is not a JSON value")


def _thaw(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    if isinstance(value, list):
        return [_thaw(v) for v in value]
    if isinstance(value, dict):
        return {k: _thaw(v) for k, v in value.items()}
    return value


def _u64_bytes(values: Iterable[int]) -> bytes:
    a = array("Q", values)
    if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only
        a.byteswap()
    return a.tobytes()


def _u64_array(data: bytes | bytearray | memoryview, what: str) -> array:
    if len(data) % 8:
        raise ValueError(f"{what}: blob length is not a multiple of 8")
    a = array("Q")
    a.frombytes(data)
    if _BIG_ENDIAN:  # pragma: no cover
        a.byteswap()
    return a


def _uint(v: Any, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        try:
            v = operator.index(v)
        except TypeError:
            raise ValueError(f"{what}: expected an integer, got {type(v).__name__}") from None
    if not 0 <= v <= _U64_MAX:
        raise ValueError(f"{what}: {v} out of range")
    return int(v)


def _span(value: Any, what: str) -> tuple[int, int] | None:
    if value is None:
        return None
    try:
        s, e = value
    except (TypeError, ValueError):
        raise ValueError(f"{what}: a span must be (start, end), got {value!r}"[:200]) from None
    s, e = _uint(s, what), _uint(e, what)
    if s > e:
        raise ValueError(f"{what}: span ({s}, {e}) is reversed")
    return s, e


def _triples(values: Any, what: str) -> list[tuple[int, int, int]]:
    out = []
    for item in values or ():
        try:
            pos, rid, n = item
        except (TypeError, ValueError):
            raise ValueError(f"{what}: entries are (pos, rid_lo, n)") from None
        pos, rid, n = _uint(pos, what), _uint(rid, what), _uint(n, what)
        if n == 0 or rid + n > _U64_MAX:
            raise ValueError(f"{what}: invalid entry ({pos}, {rid}, {n})")
        out.append((pos, rid, n))
    return out


# ----------------------------------------------------------------------------------
# structure helpers
# ----------------------------------------------------------------------------------
def _op_inserted(op: tuple) -> int:
    if op[0] == "ins":
        return op[2]
    if op[0] == "inss":
        return len(op[1])
    return 0


def _op_removed(op: tuple) -> int:
    if op[0] == "del":
        return op[2]
    if op[0] == "dels":
        return len(op[1])
    return 0


def _split_rids(entries: Sequence[tuple[int, int, int]], counts: Sequence[int]) -> list[list[tuple[int, int, int]]]:
    """Split ``new_rids`` entries into the chunks consumed by each op (``counts`` = items per op)."""
    out: list[list[tuple[int, int, int]]] = []
    i = 0
    for want in counts:
        chunk = []
        got = 0
        while got < want:
            if i >= len(entries):
                raise ValueError("new_rids: not enough entries for the inserted items")
            entry = entries[i]
            i += 1
            if got + entry[2] > want:
                raise ValueError("new_rids: an entry covers more items than its op inserts")
            chunk.append(entry)
            got += entry[2]
        out.append(chunk)
    if i != len(entries):
        raise ValueError("new_rids: more entries than inserted items")
    return out


def _rid_groups(positions: array, rids: array) -> list[tuple[int, int, int]]:
    """``(position of the first item, rid_lo, n)`` per run of consecutive rids."""
    n = len(rids)
    if not n:
        return []
    breaks = list(compress(range(1, n), map(ne, islice(rids, 1, None), map(add, rids, repeat(1)))))
    starts = [0, *breaks]
    ends = [*breaks, n]
    return [(positions[a], rids[a], b - a) for a, b in zip(starts, ends)]


def removed_rids(pieces: PieceTable, struct: Sequence[tuple], new_rids: Sequence[tuple[int, int, int]] = ()
                 ) -> list[tuple[int, int, int]]:
    """``Step.old_rids`` of the forward ops ``struct`` applied to the pre-step table ``pieces``: the rids
    of the items every "del"/"dels" op removes, as ``(pos, rid_lo, n)`` entries ordered for
    ``pieces_after.apply(Remap(struct).inverse(), new_rids=...)`` (removing ops in reverse order, items
    in ascending position order). ``ValueError``/``IndexError`` when the ops do not fit the table."""
    ops = list(struct)
    chunks = _split_rids(list(new_rids), [_op_inserted(op) for op in ops])
    per_op: list[list[tuple[int, int, int]]] = []
    tbl = pieces
    for op, chunk in zip(ops, chunks):
        kind = op[0]
        if kind == "del":
            pos, n = op[1], op[2]
            if pos + n > tbl.count:
                raise IndexError(f"delete range {pos}+{n} out of range (count {tbl.count})")
            per_op.append([(p, r, k) for p, r, k in tbl.runs(pos, pos + n)])
        elif kind == "dels":
            positions = op[1]
            per_op.append(_rid_groups(positions, tbl.rids_at(positions)))
        else:
            per_op.append([])
        tbl = tbl.apply(Remap([op]), new_rids=chunk)
    out: list[tuple[int, int, int]] = []
    for entries in reversed(per_op):
        out.extend(entries)
    return out


def _max_rid(tbl: PieceTable) -> int:
    """Largest rid of a table + 1 (0 when empty)."""
    m = 0
    for _p, r, n in tbl.runs():
        if r + n > m:
            m = r + n
    return m


# ----------------------------------------------------------------------------------
# Step
# ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class StepInfo:
    """One row of the history (:meth:`EditStore.steps`): ``label`` is localized."""

    seq: int
    ts: float
    kind: str
    label: str


def step_label(label_key: str, label_params: Mapping[str, Any] | None = None) -> str:
    """The localized label of a step (``edits.step.*`` keys); an unknown key gives a generic label."""
    if not label_key or not has(label_key):
        return t("editstore.step.unknown")
    return t(label_key, **dict(label_params or {}))


@dataclass(eq=False)
class Step:
    """One undoable step: a symmetric delta between two states (§12.2). Spans are virtual.

    * ``content``: ``(rid, old, new)`` top-DictLayer entries (``None`` = absent), unique rids; applied
      before ``push_layer``;
    * ``struct``: forward Remap ops (canonical ``Remap(...).ops``) of the record structure, with
      ``new_rids`` for "ins"/"inss" and ``old_rids`` (filled automatically) for "del"/"dels";
    * ``pieces_blob``: span of a full piece-table snapshot AFTER the step (set by the store);
    * ``push_layer``: an array-layer file of the layer folder pushed on top (then an empty DictLayer);
    * ``segments``: segments created by this step (deleted with it when it is truncated as redo);
    * ``wrapper``: ``(old span, new span, old hole, new hole)``; ``flags``: ``(old, new)`` state flags
      (``None`` = unchanged);
    * ``nodes`` (NodeChange list), ``records`` (changed positions after the step, ``None`` = many),
      ``keys`` (``(rid, old key, new key)``), ``select``: ChangeSet data kept for undo/redo and patch
      export (``select`` is stored as nested tuples);
    * ``seq`` / ``ts``: set by :meth:`EditStore.commit`.

    Lists longer than 4096 entries are stored as blobs in segment 0; :meth:`to_json` /
    :meth:`from_json` round-trip every field exactly."""

    kind: str
    label_key: str
    label_params: dict = field(default_factory=dict)
    content: list = field(default_factory=list)
    struct: list | None = None
    pieces_blob: tuple | None = None
    push_layer: str | None = None
    segments: list = field(default_factory=list)
    wrapper: tuple | None = None
    flags: tuple | None = None
    nodes: list | None = None
    records: list | None = None
    keys: list | None = None
    select: Any = None
    seq: int = 0
    ts: float = 0.0
    new_rids: list = field(default_factory=list)
    old_rids: list | None = None
    next_rid: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("Step.kind must be a non-empty string")
        if not isinstance(self.label_key, str):
            raise ValueError("Step.label_key must be a string")
        self.label_params = _json_value(dict(self.label_params or {}), "Step.label_params")
        content = []
        seen: set[int] = set()
        for item in self.content or ():
            try:
                rid, old, new = item
            except (TypeError, ValueError):
                raise ValueError("Step.content entries are (rid, old, new)") from None
            rid = _uint(rid, "Step.content rid")
            if rid in seen:
                raise ValueError(f"Step.content: rid {rid} occurs twice")
            seen.add(rid)
            content.append((rid, _span(old, "Step.content old"), _span(new, "Step.content new")))
        self.content = content
        self._norm_struct()
        self.pieces_blob = _span(self.pieces_blob, "Step.pieces_blob")
        if self.push_layer is not None:
            name = self.push_layer
            if not isinstance(name, str) or not name or Path(name).name != name or name in (".", ".."):
                raise ValueError(f"Step.push_layer must be a file name, got {name!r}")
        segs = []
        for sg in self.segments or ():
            sg = _uint(sg, "Step.segments")
            if not 1 <= sg <= MAX_SEGMENT:
                raise ValueError(f"Step.segments: invalid segment {sg}")
            if sg not in segs:
                segs.append(sg)
        self.segments = segs
        if self.wrapper is not None:
            try:
                ow, nw, oh, nh = self.wrapper
            except (TypeError, ValueError):
                raise ValueError("Step.wrapper is (old span, new span, old hole, new hole)") from None
            self.wrapper = (_span(ow, "Step.wrapper"), _span(nw, "Step.wrapper"), _span(oh, "Step.wrapper"),
                            _span(nh, "Step.wrapper"))
        if self.flags is not None:
            try:
                fo, fn = self.flags
            except (TypeError, ValueError):
                raise ValueError("Step.flags is (old, new) or None") from None
            self.flags = (_uint(fo, "Step.flags"), _uint(fn, "Step.flags"))
        if self.nodes is not None:
            nodes = []
            for nc in self.nodes:
                if not isinstance(nc, NodeChange):
                    try:
                        rec, ipath, remap, count = nc
                    except (TypeError, ValueError):
                        raise ValueError("Step.nodes holds NodeChange objects") from None
                    nc = NodeChange(rec, tuple(ipath), remap if isinstance(remap, Remap) else Remap(remap), count)
                nodes.append(nc)
            self.nodes = nodes
        if self.records is not None:
            self.records = [_uint(p, "Step.records") for p in self.records]
        if self.keys is not None:
            keys = []
            for item in self.keys:
                try:
                    rid, old, new = item
                except (TypeError, ValueError):
                    raise ValueError("Step.keys entries are (rid, old key, new key)") from None
                for k in (old, new):
                    if k is not None and not isinstance(k, str):
                        raise ValueError("Step.keys: keys are strings or None")
                keys.append((_uint(rid, "Step.keys rid"), old, new))
            self.keys = keys
        self.select = None if self.select is None else _json_value(self.select, "Step.select", freeze=True)
        self.seq = _uint(self.seq, "Step.seq")
        if isinstance(self.ts, bool) or not isinstance(self.ts, (int, float)) or not math.isfinite(self.ts):
            raise ValueError("Step.ts must be a finite number")
        self.ts = float(self.ts)
        self.next_rid = _uint(self.next_rid, "Step.next_rid")

    def _norm_struct(self) -> None:
        struct = self.struct
        new_rids = self.new_rids
        old_rids = self.old_rids
        remap: Remap | None = None
        if struct is not None:
            if isinstance(struct, Remap):
                remap = struct
            elif isinstance(struct, (list, tuple)) and struct and isinstance(struct[0], Remap):
                remap = struct[0]
                if len(struct) > 1 and not new_rids:
                    new_rids = struct[1]
                if len(struct) > 2 and old_rids is None:
                    old_rids = struct[2]
            else:
                remap = Remap(struct)
            if remap.identity:
                remap = None
        self.new_rids = _triples(new_rids, "Step.new_rids")
        self.old_rids = None if old_rids is None else _triples(old_rids, "Step.old_rids")
        if remap is None:
            if self.new_rids or self.old_rids:
                raise ValueError("Step: new_rids/old_rids without structural ops")
            self.struct = None
            self._remap: Remap | None = None
            self._remap_src: Any = None
            return
        ops = list(remap.ops)
        inserted = sum(_op_inserted(op) for op in ops)
        if sum(e[2] for e in self.new_rids) != inserted:
            raise ValueError(f"Step.new_rids cover {sum(e[2] for e in self.new_rids)} items, the ops insert {inserted}")
        removed = sum(_op_removed(op) for op in ops)
        if self.old_rids is not None and sum(e[2] for e in self.old_rids) != removed:
            raise ValueError(f"Step.old_rids cover {sum(e[2] for e in self.old_rids)} items, the ops remove {removed}")
        if not removed and self.old_rids is None:
            self.old_rids = []
        self.struct = ops
        self._remap = remap
        self._remap_src = ops

    # -- derived -----------------------------------------------------------------------
    @property
    def remap(self) -> Remap | None:
        """The structural ops as a :class:`Remap` (``None`` for a content-only step)."""
        if self.struct is None:
            return None
        if self._remap_src is not self.struct:   # struct replaced after construction
            self._norm_struct()
        return self._remap

    @property
    def structural(self) -> bool:
        return self.struct is not None

    @property
    def label(self) -> str:
        return step_label(self.label_key, self.label_params)

    def fill_old_rids(self, pieces: PieceTable) -> None:
        """Compute ``old_rids`` from the pre-step piece table when they are missing."""
        remap = self.remap
        if remap is None or self.old_rids is not None:
            return
        self.old_rids = removed_rids(pieces, remap.ops, self.new_rids)

    # -- equality ------------------------------------------------------------------------
    def _cmp(self) -> tuple:
        nodes = None if self.nodes is None else [(n.record, n.ipath, n.remap.ops, n.count) for n in self.nodes]
        return (self.kind, self.label_key, self.label_params, self.content, self.struct, self.pieces_blob,
                self.push_layer, self.segments, self.wrapper, self.flags, nodes, self.records, self.keys,
                self.select, self.seq, self.ts, self.new_rids, self.old_rids, self.next_rid)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Step):
            return NotImplemented
        return self._cmp() == other._cmp()

    __hash__ = None  # type: ignore[assignment]

    # -- JSON ------------------------------------------------------------------------------
    def to_json(self, blob_writer: Callable[[bytes], Sequence[int]] | None = None) -> dict:
        """JSON-ready dict (``seq``/``ts`` excluded: they are columns). Lists longer than 4096 entries
        go through ``blob_writer(bytes) -> (vstart, vend)`` when one is given."""

        def blob(data: bytes, **extra: Any) -> dict:
            assert blob_writer is not None
            span = blob_writer(data)
            return {"blob": [int(span[0]), int(span[1])], **extra}

        def ints(values: list[int]) -> Any:
            if blob_writer is not None and len(values) > LIST_BLOB_MIN:
                return blob(_u64_bytes(values))
            return list(values)

        def triples(values: list[tuple[int, int, int]]) -> Any:
            if blob_writer is not None and len(values) > LIST_BLOB_MIN:
                return blob(_u64_bytes(x for tr in values for x in tr), k=3)
            return [list(tr) for tr in values]

        def json_list(values: list) -> Any:
            if blob_writer is not None and len(values) > LIST_BLOB_MIN:
                return blob(_dumps(values).encode("utf-8"), enc="json")
            return values

        def sp(s: tuple[int, int] | None) -> list[int] | None:
            return None if s is None else [s[0], s[1]]

        data: dict[str, Any] = {
            "v": 1,
            "kind": self.kind,
            "label": [self.label_key, self.label_params],
            "content": json_list([[rid, sp(old), sp(new)] for rid, old, new in self.content]),
            "struct": None if self.struct is None else self.remap.to_json(blob_writer),  # type: ignore[union-attr]
            "new_rids": triples(self.new_rids),
            "old_rids": None if self.old_rids is None else triples(self.old_rids),
            "pieces_blob": sp(self.pieces_blob),
            "push_layer": self.push_layer,
            "segments": list(self.segments),
            "wrapper": None if self.wrapper is None else [sp(x) for x in self.wrapper],
            "flags": None if self.flags is None else list(self.flags),
            "nodes": None if self.nodes is None else json_list(
                [[n.record, list(n.ipath), n.remap.to_json(blob_writer), n.count] for n in self.nodes]),
            "records": None if self.records is None else ints(self.records),
            "keys": None if self.keys is None else json_list([list(k) for k in self.keys]),
            "select": None if self.select is None else _thaw(self.select),
            "next_rid": self.next_rid,
        }
        return data

    @classmethod
    def from_json(cls, data: Any, blob_reader: Callable[[int, int], bytes] | None = None, *, seq: int = 0,
                  ts: float = 0.0) -> Step:
        """Inverse of :meth:`to_json`; ``blob_reader(vstart, vend) -> bytes`` resolves blobs. Invalid data
        raises ``ValueError``."""
        if not isinstance(data, dict):
            raise ValueError("step JSON must be an object")

        def read_blob(ref: dict) -> bytes:
            span = ref.get("blob")
            if not isinstance(span, list) or len(span) != 2:
                raise ValueError("step JSON: invalid blob reference")
            if blob_reader is None:
                raise ValueError("step JSON references a blob but no blob_reader was given")
            return blob_reader(_uint(span[0], "blob"), _uint(span[1], "blob"))

        def ints(v: Any) -> list[int]:
            if isinstance(v, dict):
                return _u64_array(read_blob(v), "step blob").tolist()
            if not isinstance(v, list):
                raise ValueError("step JSON: expected a list")
            return v

        def triples(v: Any) -> list:
            if isinstance(v, dict):
                flat = _u64_array(read_blob(v), "step blob")
                if len(flat) % 3:
                    raise ValueError("step JSON: triple blob of invalid length")
                return [tuple(flat[i:i + 3]) for i in range(0, len(flat), 3)]
            if not isinstance(v, list):
                raise ValueError("step JSON: expected a list")
            return v

        def json_list(v: Any) -> list:
            if isinstance(v, dict):
                if v.get("enc") != "json":
                    raise ValueError("step JSON: invalid list blob")
                out = _loads(read_blob(v))
                if not isinstance(out, list):
                    raise ValueError("step JSON: list blob does not hold a list")
                return out
            if not isinstance(v, list):
                raise ValueError("step JSON: expected a list")
            return v

        try:
            label = data["label"]
            if not isinstance(label, list) or len(label) != 2 or not isinstance(label[1], dict):
                raise ValueError("step JSON: invalid label")
            struct_json = data.get("struct")
            remap = None if struct_json is None else Remap.from_json(struct_json, blob_reader)
            nodes = None
            if data.get("nodes") is not None:
                nodes = []
                for item in json_list(data["nodes"]):
                    rec, ipath, rj, count = item
                    nodes.append(NodeChange(rec, tuple(ipath), Remap.from_json(rj, blob_reader), count))
            wrapper = data.get("wrapper")
            old_rids = data.get("old_rids")
            step = cls(
                kind=data["kind"],
                label_key=label[0],
                label_params=label[1],
                content=[(rid, old, new) for rid, old, new in json_list(data.get("content", []))],
                struct=None if remap is None else list(remap.ops),
                pieces_blob=data.get("pieces_blob"),
                push_layer=data.get("push_layer"),
                segments=data.get("segments") or [],
                wrapper=None if wrapper is None else tuple(wrapper),
                flags=None if data.get("flags") is None else tuple(data["flags"]),
                nodes=nodes,
                records=None if data.get("records") is None else ints(data["records"]),
                keys=None if data.get("keys") is None else [tuple(k) for k in json_list(data["keys"])],
                select=data.get("select"),
                seq=seq,
                ts=ts,
                new_rids=triples(data.get("new_rids", [])),
                old_rids=None if old_rids is None else triples(old_rids),
                next_rid=data.get("next_rid", 0),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"step JSON: {exc!r}") from exc
        return step


def step_next_rid(step: Step) -> int:
    """The high-water mark of rids used by ``step`` (explicit ``next_rid``, new rids, content rids,
    "pieces" tables)."""
    m = step.next_rid
    for _pos, rid, n in step.new_rids:
        if rid + n > m:
            m = rid + n
    for rid, _old, new in step.content:
        if new is not None and rid + 1 > m:
            m = rid + 1
    if step.struct is not None:
        for op in step.struct:
            if op[0] == "pieces":
                m = max(m, _max_rid(op[2]))
    return m


def _needs_old_rids(step: Step) -> bool:
    return step.struct is not None and any(op[0] in ("del", "dels") for op in step.struct)


# ----------------------------------------------------------------------------------
# apply / undo
# ----------------------------------------------------------------------------------
LayersArg = Any   # EditStore | Mapping[str, ArrayLayer] | Callable[[str], ArrayLayer] | None


def _resolve_layer(layers: LayersArg, name: str) -> ArrayLayer:
    if layers is None:
        raise ValueError(f"step pushes the array layer {name!r} but no layers were given")
    if isinstance(layers, EditStore):
        return layers.layer(name)
    if isinstance(layers, Mapping):
        layer = layers.get(name)
        if layer is None:
            raise ValueError(f"array layer {name!r} is not available")
        return layer
    return layers(name)


def apply_step(state: OverlayState, step: Step, *, layers: LayersArg = None) -> OverlayState:
    """The state after ``step`` (``version = step.seq``; ``next_rid`` never lowered).

    ``layers`` resolves ``push_layer`` names (an :class:`EditStore`, a mapping name -> ArrayLayer or a
    callable). Fills ``step.old_rids`` from ``state`` when missing. A step that does not fit the state
    raises :class:`LayerDamagedError`."""
    if step.struct is not None and step.old_rids is None:
        try:
            step.fill_old_rids(state.pieces)
        except (ValueError, IndexError) as exc:
            raise _damaged(step.seq, f"structure does not fit: {exc}") from exc
    pieces = state.pieces
    remap = step.remap
    if remap is not None:
        try:
            pieces = pieces.apply(remap, new_rids=step.new_rids)
        except (ValueError, IndexError) as exc:
            raise _damaged(step.seq, f"structure does not fit: {exc}") from exc
    top = state.layers[-1]
    changes: dict[int, tuple[int, int] | None] = {}
    for rid, old, new in step.content:
        if top.get(rid) != old:
            raise _damaged(step.seq, f"record {rid}: the content does not match the step")
        changes[rid] = new
    new_layers = state.layers[:-1] + (top.with_changes(changes),)
    if step.push_layer is not None:
        try:
            arr = _resolve_layer(layers, step.push_layer)
        except (ValueError, EditError) as exc:
            if isinstance(exc, LayerDamagedError):
                raise
            raise _damaged(step.seq, f"array layer {step.push_layer}: {exc}") from exc
        new_layers = new_layers + (arr, DictLayer())
    wrapper, hole = state.wrapper, state.wrapper_hole
    if step.wrapper is not None:
        ow, nw, oh, nh = step.wrapper
        if (wrapper, hole) != (ow, oh):
            raise _damaged(step.seq, "the wrapper does not match the step")
        wrapper, hole = nw, nh
    return state.derive(pieces=pieces, layers=new_layers, wrapper=wrapper, wrapper_hole=hole,
                        flags=state.flags if step.flags is None else step.flags[1],
                        next_rid=max(state.next_rid, step_next_rid(step)),
                        version=step.seq)


def undo_step(state: OverlayState, step: Step, *, layers: LayersArg = None) -> OverlayState:
    """The state before ``step`` given the state after it (``version = step.seq - 1``; ``next_rid`` is
    kept: undo never lowers it). A pushed array layer is popped with the (empty) DictLayer above it."""
    cur = state.layers
    if step.push_layer is not None:
        if (len(cur) < 3 or len(cur[-1]) or not isinstance(cur[-2], ArrayLayer)
                or cur[-2].name != step.push_layer):
            raise _damaged(step.seq, f"the array layer {step.push_layer} is not on top")
        cur = cur[:-2]
    top = cur[-1]
    changes: dict[int, tuple[int, int] | None] = {}
    for rid, old, new in step.content:
        if top.get(rid) != new:
            raise _damaged(step.seq, f"record {rid}: the content does not match the step")
        changes[rid] = old
    new_layers = cur[:-1] + (top.with_changes(changes),)
    pieces = state.pieces
    remap = step.remap
    if remap is not None:
        if step.old_rids is None and _needs_old_rids(step):
            raise _damaged(step.seq, "the step does not know the removed records")
        try:
            pieces = pieces.apply(remap.inverse(), new_rids=step.old_rids or ())
        except (ValueError, IndexError) as exc:
            raise _damaged(step.seq, f"structure does not fit: {exc}") from exc
    wrapper, hole = state.wrapper, state.wrapper_hole
    if step.wrapper is not None:
        ow, nw, oh, nh = step.wrapper
        if (wrapper, hole) != (nw, nh):
            raise _damaged(step.seq, "the wrapper does not match the step")
        wrapper, hole = ow, oh
    return state.derive(pieces=pieces, layers=new_layers, wrapper=wrapper, wrapper_hole=hole,
                        flags=state.flags if step.flags is None else step.flags[0], next_rid=state.next_rid,
                        version=max(0, step.seq - 1))


# ----------------------------------------------------------------------------------
# keys, folders, notices
# ----------------------------------------------------------------------------------
def _record_path_json(rp: Any) -> list | None:
    if rp is None:
        return None
    return [seg if isinstance(seg, int) and not isinstance(seg, bool) else str(seg) for seg in rp]


def _base_count(doc: Any) -> int:
    if not getattr(doc, "index_complete", True):
        raise EditError("editstore.error.not_indexed")
    base = getattr(doc, "base_record_count", None)
    if base is None:
        base = doc.record_count
    return int(base)


def layer_key(doc: Any) -> str:
    """sha256 hex[:32] of (identity digest, kind, record path text, base record count)."""
    rp = _record_path_json(doc.info.record_path)
    rp_text = "" if rp is None else json.dumps(rp, ensure_ascii=True, separators=(",", ":"))
    payload = json.dumps([doc.identity.digest(), doc.info.kind, rp_text, _base_count(doc)], ensure_ascii=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()[:32]


def _edits_root() -> Path:
    try:
        return edits_dir()
    except JsonReshapeError as exc:
        raise _unavailable(exc) from exc


def _locks_root() -> Path:
    try:
        return edits_locks_dir()
    except JsonReshapeError as exc:
        raise _unavailable(exc) from exc


def beside_dir(doc: Any) -> Path:
    """``<file>.jredit`` next to the document's source file."""
    src = Path(doc.source_path)
    return src.parent / (src.name + BESIDE_SUFFIX)


def _location_of(path: Path, doc: Any) -> str:
    """"beside" / "appdata" when ``path`` is one of the two standard places of a layer, else "custom"."""
    norm = normalize_path(path)
    if norm == normalize_path(beside_dir(doc)):
        return "beside"
    try:
        root = edits_dir()
    except JsonReshapeError:
        return "custom"
    if _KEY_RE.fullmatch(path.name) and normalize_path(path.parent) == normalize_path(root):
        return "appdata"
    return "custom"


def lock_path(doc: Any) -> Path:
    """``edits_dir()/locks/<key>.lock`` (``doc`` may also be a layer key)."""
    key = doc if isinstance(doc, str) else layer_key(doc)
    return _locks_root() / f"{key}.lock"


def file_lock_key(path: Any) -> str | None:
    """The lock key of the PHYSICAL file at ``path`` (or of ``path.source_path`` for a document): sha256 hex[:32]
    of its volume serial number and file id (``GetFileInformationByHandle``, i.e. ``os.stat`` ``st_dev`` /
    ``st_ino``), so every spelling of one file -- a directory junction or symlink, a ``subst`` drive, a mapped
    drive and its UNC path -- gives the same key. A file system without file ids falls back to the resolved path
    (``os.path.realpath``); ``None`` when the file cannot be inspected at all."""
    src = getattr(path, "source_path", path)
    try:
        st = os.stat(long_path(src))
    except (OSError, ValueError):
        st = None
    if st is not None and st.st_ino:
        ident = f"file:{st.st_dev}:{st.st_ino}"
    else:
        try:
            ident = "path:" + normalize_path(os.path.realpath(long_path(src)))
        except (OSError, ValueError):
            return None
    return hashlib.sha256(ident.encode("utf-8", "surrogatepass")).hexdigest()[:32]


_NOTICE_LOCK = threading.Lock()
_NOTICES: list[tuple[str, dict[str, Any]]] = []
_NOTED: set[str] = set()


def _note_fallback(path: Path, fallback: Path, detail: str) -> tuple[str, dict[str, Any]]:
    params = {"path": str(path), "fallback": str(fallback), "detail": detail}
    notice = ("editstore.warn.beside_fallback", params)
    with _NOTICE_LOCK:
        norm = normalize_path(path)
        if norm not in _NOTED:
            _NOTED.add(norm)
            _NOTICES.append(notice)
    log.warning("edit layer cannot be kept next to the file (%s): %s; using %s", path, detail, fallback)
    return notice


def take_notices() -> list[tuple[str, dict[str, Any]]]:
    """Fallback notices recorded since the last call (``(i18n key, params)``; once per folder per
    process), like :func:`jsonreshape.appdirs.take_notices`."""
    with _NOTICE_LOCK:
        out = list(_NOTICES)
        _NOTICES.clear()
    return out


def notice_texts() -> list[str]:
    return [t(key, **params) for key, params in take_notices()]


def _reason(exc: BaseException, path: Path) -> str:
    if isinstance(exc, JsonReshapeError):
        return exc.message
    if isinstance(exc, OSError):
        return os_error(exc, path, writing=True).message
    return str(exc)


def _probe_dir(path: Path) -> str | None:
    """``None`` when a layer folder could be created at ``path`` (checked with a probe file in the
    folder, or in its parent when it does not exist yet -- nothing is left behind), else the reason."""
    target = path if _isdir(path) else path.parent
    if _exists(path) and not _isdir(path):
        return _reason(FileExistsError(_errno.EEXIST, "not a folder", str(path)), path)
    probe = target / f".{path.name[:40]}.probe-{os.getpid()}-{secrets.token_hex(4)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
    try:
        fd = os.open(long_path(probe), flags, 0o666)
        os.close(fd)
    except OSError as exc:
        return _reason(exc, path)
    try:
        os.remove(long_path(probe))
    except OSError:  # pragma: no cover - a scanner holds the new file
        pass
    return None


def layer_dir(doc: Any, *, settings: Settings | None = None) -> Path:
    """Where a NEW layer of ``doc`` goes: ``edits_dir()/<key>`` or, with ``edits_location == "beside"``,
    ``<file>.jredit`` when a folder can be made there (else the app folder, with the notice
    ``editstore.warn.beside_fallback``)."""
    s = settings if settings is not None else get_settings()
    key = layer_key(doc)
    if s.edits_location == "beside":
        bdir = beside_dir(doc)
        problem = _probe_dir(bdir)
        if problem is None:
            return bdir
        app = _edits_root() / key
        _note_fallback(bdir, app, problem)
        return app
    return _edits_root() / key


# ----------------------------------------------------------------------------------
# file helpers
# ----------------------------------------------------------------------------------
def _exists(p: Path) -> bool:
    try:
        return os.path.exists(long_path(p))
    except (OSError, ValueError):
        return False


def _isdir(p: Path) -> bool:
    try:
        return os.path.isdir(long_path(p))
    except (OSError, ValueError):
        return False


def _isfile(p: Path) -> bool:
    try:
        return os.path.isfile(long_path(p))
    except (OSError, ValueError):
        return False


def _size(p: Path) -> int | None:
    try:
        return os.stat(long_path(p)).st_size
    except (OSError, ValueError):
        return None


def _remove(p: Path) -> bool:
    try:
        os.remove(long_path(p))
        return True
    except FileNotFoundError:
        return True
    except OSError:
        log.debug("cannot remove %s", p, exc_info=True)
        return False


def _listdir(p: Path) -> list[str]:
    try:
        return os.listdir(long_path(p))
    except OSError:
        return []


def _layer_file_names(names: Iterable[str]) -> list[str]:
    """Files of a layer folder that this module owns (database, segments, layers, temporaries)."""
    return [n for n in names if n in _DB_FILES or _SEG_FILE.fullmatch(n) or _LAYER_FILE.fullmatch(n)
            or _TEMP_FILE.fullmatch(n)]


def _purge_dir(path: Path) -> bool:
    """Delete a layer folder (database first, then segments/layers/temporaries; files this module does
    not know are kept). Segment and array-layer files that alive states of this process may still read
    are deleted later (:func:`_defer_delete`). True when nothing of the layer is left (deferred files
    count as gone: they are deleted as soon as nothing can read them)."""
    names = _listdir(path)
    ok = True
    for name in _DB_FILES:
        if name in names and not _remove(path / name):
            ok = False
    files = [n for n in _layer_file_names(names) if n not in _DB_FILES and not _is_retained(path / n)]
    readable = [n for n in files if _SEG_FILE.fullmatch(n) or _LAYER_FILE.fullmatch(n)]
    for name in files:
        if name not in readable and not _remove(path / name):
            ok = False
    if readable and not _defer_delete(path, readable, _witnesses(path), rmdir=True):
        ok = False
    _rmdir_if_empty(path)
    if _isdir(path) and _layer_file_names(_listdir(path)):
        ok = ok and all(_is_retained(path / n) for n in _layer_file_names(_listdir(path)))
    return ok


def _rmdir_if_empty(path: Path) -> None:
    try:
        os.rmdir(long_path(path))
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("layer folder %s not removed (files left)", path)


# ----------------------------------------------------------------------------------
# file names never reused in this process; deferred deletion of files alive states may read
# ----------------------------------------------------------------------------------
# A new layer created in a folder this process used before (a discard followed by a new edit, a layer
# deleted and made again) continues the segment and layer-file numbers: a reader of a state of the old
# layer that opens a segment by its name (lazily, per thread) can never get the new layer's bytes, and
# virtual offsets stay unique per folder (docs/ARCHITECTURE.md §12.0).
_NAMES_LOCK = threading.Lock()
_FOLDER_NEXT: dict[str, list[int]] = {}       # normalize_path(folder) -> [next segment, next layer file]


def _claim_numbers(folder: Path, seg_floor: int, layer_floor: int) -> None:
    """Raise ``folder``'s process-wide counters to at least the given numbers."""
    norm = normalize_path(folder)
    with _NAMES_LOCK:
        cur = _FOLDER_NEXT.setdefault(norm, [0, 1])
        cur[0] = max(cur[0], seg_floor)
        cur[1] = max(cur[1], layer_floor)


def _take_number(folder: Path, which: int) -> int:
    """The next unused segment (``which=0``) or layer-file (``which=1``) number of ``folder``."""
    norm = normalize_path(folder)
    with _NAMES_LOCK:
        cur = _FOLDER_NEXT.setdefault(norm, [0, 1])
        n = cur[which]
        cur[which] = n + 1
        return n


def _numbers_in(names: Iterable[str]) -> tuple[int, int]:
    """(1 + the largest segment number, 1 + the largest layer-file number) among ``names`` (0 / 1 when none)."""
    segs = [int(m.group(1)) for m in map(_SEG_FILE.fullmatch, names) if m]
    lays = [int(m.group(1)) for m in map(_LAYER_FILE.fullmatch, names) if m]
    return max([-1, *segs]) + 1, max([0, *lays]) + 1


# Deferred deletion (CL-2/CL-10 of the stage-3 review). A layer file that alive states may still read -- the
# private files of steps truncated as redo, every file of a discarded / saved / head-0 layer -- is not deleted
# while a "witness" lives: an OverlayState of this process that reads layer bytes, whose segment reader belongs
# to the folder and is owned by a Document (set_overlay, §12.2; states of the truncated steps only: their version
# is above the head), and whose reader is not closed (a closed reader -- Document.close() -- cannot read anything
# any more). Pinned views and leases keep their states, so a running export, search or preview keeps working.
# When the last witness is gone (a weakref callback or a closed reader wakes the reaper thread; no I/O happens
# in the thread that dropped the reference, which may be the GUI thread) the reaper forgets the segment in the
# readers documents own and closes their array layers of the file (handles and mappings keep a deleted file's
# disk space), then deletes the file -- unless it is not the same file any more (another process may have made
# a new layer in the folder meanwhile). Without a witness a file is released and deleted at once. Leftovers of a
# process that ended first are collected at the next open (``_gc_files``) or by find_pending.
class _Retained:
    __slots__ = ("folder", "ident", "name", "rmdir", "witnesses")

    def __init__(self, folder: Path, name: str, ident: tuple | None, witnesses: list, rmdir: bool) -> None:
        self.folder = folder
        self.name = name
        self.ident = ident
        self.witnesses = witnesses
        self.rmdir = rmdir


_RET_LOCK = threading.Lock()
_RETAINED: dict[str, _Retained] = {}          # normalize_path(file) -> entry
_REAP_QUEUE: queue.SimpleQueue = queue.SimpleQueue()
_REAPER_LOCK = threading.Lock()
_REAPER: threading.Thread | None = None


def _wake_reaper(*_args: Any) -> None:
    """Weakref callback / closed-reader hook: wake the reaper when files wait (``SimpleQueue.put`` is reentrant;
    no lock, no I/O: this may run in any thread, the GUI thread included)."""
    if _RETAINED:
        _REAP_QUEUE.put(None)


def _reaper_loop() -> None:
    while True:
        _REAP_QUEUE.get()
        while True:                 # one sweep for a burst of wake-ups
            try:
                _REAP_QUEUE.get_nowait()
            except queue.Empty:
                break
        try:
            _sweep_retained()
        except Exception:  # pragma: no cover - never let the reaper die
            log.exception("deleting retained edit-layer files failed")


def _start_reaper() -> None:
    global _REAPER
    with _REAPER_LOCK:
        if _REAPER is None or not _REAPER.is_alive():
            _REAPER = threading.Thread(target=_reaper_loop, name="jsonreshape-layer-reaper", daemon=True)
            _REAPER.start()


def _file_ident(path: Path) -> tuple | None:
    try:
        st = os.stat(long_path(path))
    except (OSError, ValueError):
        return None
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns


def _reads_layer(state: OverlayState) -> bool:
    """The state reads bytes of its edit layer (content entries or an edited wrapper)."""
    return state.wrapper is not None or any(len(layer) for layer in state.layers)


def _witnesses(folder: Path, *, above: int | None = None) -> list[OverlayState]:
    """Alive states of this process that may read ``folder``'s files (``above``: only versions above it): states
    of a segment reader a Document owns (``set_overlay``; pinned views and leases are views of documents) that is
    not closed. A caller that uses states without a document (a tool, a test) manages their lifetime itself."""
    readers = {r for r in readers_of(folder) if r.owned and not r.closed}
    if not readers:
        return []
    return [st for st in live_states()
            if st.reader in readers and (above is None or st.version > above) and _reads_layer(st)]


def _witness_alive(ref: weakref.ref) -> bool:
    st = ref()
    return st is not None and (st.reader is None or not st.reader.closed)


def _is_retained(path: Path) -> bool:
    with _RET_LOCK:
        return normalize_path(path) in _RETAINED


def _release_file(folder: Path, name: str) -> None:
    """Close the handles and mappings that documents own on ``folder/name`` (no state of theirs reads it any
    more), so deleting it frees its disk space. Readers and layers nobody adopted (a tool or test using states
    directly) are left alone: their handles keep reading the deleted file, as before."""
    m = _SEG_FILE.fullmatch(name)
    if m:
        seg = int(m.group(1))
        for reader in readers_of(folder):
            if reader.owned:
                reader.forget(seg)
        return
    if _LAYER_FILE.fullmatch(name):
        for layer in array_layers_of(folder / name):
            if layer.owned:
                layer.close()


def _defer_delete(folder: Path, names: Sequence[str], witnesses: Sequence[OverlayState], *,
                  rmdir: bool = False) -> bool:
    """Delete the layer files ``names`` of ``folder`` now -- or, while one of ``witnesses`` lives, when the
    last of them is gone (see above). False when a file could not be deleted now."""
    refs = [weakref.ref(st, _wake_reaper) for st in witnesses]
    refs = [r for r in refs if _witness_alive(r)]
    if not refs:
        ok = True
        for name in names:
            _release_file(folder, name)
            if not _remove(folder / name):
                ok = False
        return ok
    with _RET_LOCK:
        for name in names:
            path = folder / name
            key = normalize_path(path)
            cur = _RETAINED.get(key)
            if cur is not None:
                cur.witnesses.extend(refs)
                cur.rmdir = cur.rmdir or rmdir
                continue
            _RETAINED[key] = _Retained(folder, name, _file_ident(path), list(refs), rmdir)
    log.debug("deleting %d file(s) of %s when %d state(s) are gone", len(names), folder, len(refs))
    _start_reaper()
    _REAP_QUEUE.put(None)       # a witness that died before its entry was registered is noticed too
    return True


_SWEEP_LOCK = threading.Lock()      # one sweep at a time (a caller's sweep waits for the reaper's)


def _sweep_retained(*, force: bool = False) -> int:
    """Delete the retained files no alive state can read any more (``force``: all of them, at exit).
    Returns the number of files handled."""
    with _SWEEP_LOCK:
        return _sweep_locked(force)


def _sweep_locked(force: bool) -> int:
    with _RET_LOCK:
        due = [(key, e) for key, e in _RETAINED.items() if force or not any(map(_witness_alive, e.witnesses))]
        for key, _e in due:
            del _RETAINED[key]
    for _key, e in due:
        path = e.folder / e.name
        if not force:
            _release_file(e.folder, e.name)
        ident = _file_ident(path)
        if ident is None:
            pass                                    # already gone
        elif ident != e.ident:                      # retained files never change: another layer's file now
            log.info("%s is not the retained file any more; left alone", path)
        else:
            _remove(path)
        if e.rmdir and not _isfile(e.folder / DB_NAME):
            _rmdir_if_empty(e.folder)
    return len(due)


def retained_files() -> list[Path]:
    """Layer files whose deletion waits for alive states (diagnostics and tests)."""
    with _RET_LOCK:
        return [e.folder / e.name for e in _RETAINED.values()]


def _at_exit() -> None:  # pragma: no cover - interpreter shutdown
    try:
        _sweep_retained(force=True)
    except Exception:
        pass


atexit.register(_at_exit)
set_reader_closed_hook(_wake_reaper)


# ----------------------------------------------------------------------------------
# LayerLock
# ----------------------------------------------------------------------------------
if _WINDOWS:
    import msvcrt

    def _os_try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _os_unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover - closing the handle releases it anyway
            pass
else:  # pragma: no cover - POSIX
    import fcntl

    def _os_try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _os_unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


_LOCK_FLAGS = (os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
               | getattr(os, "O_CLOEXEC", 0))


class _Held:
    """Registry entry of a lock held by this process (the OS handle)."""

    __slots__ = ("fd", "path")

    def __init__(self, fd: int, path: Path) -> None:
        self.fd = fd
        self.path = path


_REG = threading.Condition(threading.RLock())
_HELD: dict[str, _Held] = {}


def _os_lock(path: Path) -> int | None:
    """Open and lock ``path`` (non-blocking): the fd, or None when another holder has it."""
    try:
        fd = os.open(long_path(path), _LOCK_FLAGS, 0o666)
    except PermissionError:
        return None   # Windows: the file is being deleted by its previous holder; try again
    except OSError as exc:
        raise _unavailable(exc) from exc
    if not _os_try_lock(fd):
        os.close(fd)
        return None
    if not _WINDOWS:  # pragma: no cover - POSIX: the file may have been unlinked meanwhile
        try:
            same = os.path.samestat(os.fstat(fd), os.stat(long_path(path)))
        except OSError:
            same = False
        if not same:
            _os_unlock(fd)
            os.close(fd)
            return None
    return fd


def _drop_held(key: str, held: _Held) -> None:
    """Release a registry entry (``LayerLock.release`` and the finalizer of a lost LayerLock)."""
    with _REG:
        if _HELD.get(key) is not held:
            return
        del _HELD[key]
        fd = held.fd
        if not _WINDOWS:  # pragma: no cover - POSIX: unlink while holding the lock
            _remove(held.path)
        _os_unlock(fd)
        try:
            os.close(fd)
        except OSError:  # pragma: no cover
            pass
        if _WINDOWS:  # fails harmlessly (sharing violation) while another opener has the file
            try:
                os.remove(long_path(held.path))
            except OSError:
                pass
        _REG.notify_all()


class LayerLock:
    """The inter-process lock of one layer key (``lock_path(key)``; §12.2).

    ``acquire()`` retries for :data:`LOCK_RETRY` seconds (a key held by another object of THIS process
    is waited for until it is released) and raises ``EditError("edits.error.locked")`` when it stays
    busy; ``try_acquire()`` tries once. The lock is released by :meth:`release`, when the object is
    garbage-collected, and by the OS when the process dies (never stale). An unusable lock folder
    raises ``EditError("editstore.error.unavailable")``.

    ``file_key`` (set by :meth:`for_doc`: :func:`file_lock_key` of the document's source) makes it a lock of the
    PHYSICAL file as well: the lock of that key is taken first and held together with the layer key, so the
    same file opened under two spellings (whose layer keys differ, the identity holding the normalized path)
    is never edited twice at the same time. :attr:`key` stays the layer key (layer folders and existing layers
    are unchanged)."""

    def __init__(self, key: str, *, file_key: str | None = None) -> None:
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise ValueError(f"invalid layer key {key!r}")
        self.key = key
        self._held: _Held | None = None
        self._fin: weakref.finalize | None = None
        self.file_key = file_key if file_key and file_key != key else None
        self._file_lock = LayerLock(self.file_key) if self.file_key else None

    @classmethod
    def for_doc(cls, doc: Any) -> LayerLock:
        """The lock of ``doc``'s layer AND of its physical file (see the class docstring)."""
        return cls(layer_key(doc), file_key=file_lock_key(doc))

    def __repr__(self) -> str:
        return f"<LayerLock {self.key} {'held' if self.held else 'free'}>"

    @property
    def held(self) -> bool:
        held = self._held
        if held is None or _HELD.get(self.key) is not held:
            return False
        fl = self._file_lock
        return fl is None or fl.held

    @property
    def path(self) -> Path:
        return lock_path(self.key)

    def _try_own(self) -> bool:
        cur = _HELD.get(self.key)
        if cur is not None:
            return cur is self._held
        path = lock_path(self.key)
        fd = _os_lock(path)
        if fd is None:
            return False
        held = _Held(fd, path)
        _HELD[self.key] = held
        self._held = held
        self._fin = weakref.finalize(self, _drop_held, self.key, held)
        return True

    def _try_locked(self) -> bool:
        """Both keys (the physical file first) or none (``_REG`` held)."""
        fl = self._file_lock
        if fl is None:
            return self._try_own()
        had = fl.held
        if not had and not fl._try_own():
            return False
        if self._try_own():
            return True
        if not had:
            fl.release()
        return False

    def _busy_here(self) -> bool:
        """A key this lock needs is held by another LayerLock of this process (``_REG`` held)."""
        if self.key in _HELD and _HELD[self.key] is not self._held:
            return True
        fl = self._file_lock
        return fl is not None and fl._busy_here()

    def try_acquire(self) -> bool:
        """Take the lock if it is free right now (True when this object holds it)."""
        with _REG:
            return self._try_locked()

    def acquire(self, timeout: float | None = None) -> None:
        """Take the lock, retrying for ``timeout`` seconds (default :data:`LOCK_RETRY`)."""
        wait = LOCK_RETRY if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + wait
        delay = 0.01
        with _REG:
            while True:
                if self._try_locked():
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EditError("edits.error.locked")
                if self._busy_here():      # held in this process: woken by its release
                    _REG.wait(remaining)
                else:                      # held by another process: poll
                    _REG.wait(min(delay, remaining))
                    delay = min(delay * 2, 0.1)

    def release(self) -> None:
        """Release the lock (idempotent)."""
        held, self._held = self._held, None
        fin, self._fin = self._fin, None
        if fin is not None:
            fin.detach()
        if held is not None:
            _drop_held(self.key, held)
        fl = self._file_lock
        if fl is not None:
            fl.release()

    def __enter__(self) -> LayerLock:
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()

    @staticmethod
    def is_locked(key: str) -> bool:
        """Non-blocking probe: True when the key is held by this process or another one."""
        with _REG:
            if key in _HELD:
                return True
            path = lock_path(key)
            if not _exists(path):
                return False
            fd = _os_lock(path)
            if fd is None:
                return True
            _os_unlock(fd)
            os.close(fd)
            return False


# ----------------------------------------------------------------------------------
# SQLite
# ----------------------------------------------------------------------------------
class _DbLocked(Exception):
    pass


class _DbDamaged(Exception):
    def __init__(self, reason: str, error: EditError | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error = error          # raised as it is by EditStore.open (e.g. a newer format)


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS steps(seq INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, "
    "data TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS checkpoint(id INTEGER PRIMARY KEY CHECK (id = 1), seq INTEGER NOT NULL, "
    "data TEXT NOT NULL)",
)


def _connect(path: Path, *, timeout: float) -> sqlite3.Connection:
    con = sqlite3.connect(long_path(path), isolation_level=None, check_same_thread=False, timeout=timeout)
    try:
        con.execute("PRAGMA locking_mode=EXCLUSIVE")
        con.execute("PRAGMA journal_mode=WAL").fetchone()
        con.execute("PRAGMA synchronous=FULL")
    except BaseException:
        con.close()
        raise
    return con


def _meta_text(key: str, value: Any) -> str:
    if key in _META_JSON:
        return _dumps(value)
    if key == "saving":
        return "" if value is None else _dumps(value)
    if key in _META_INT:
        return str(int(value))
    if key in _META_FLOAT:
        return repr(float(value))
    return str(value)


def _parse_meta(rows: Iterable[tuple[str, str]]) -> dict[str, Any]:
    raw = dict(rows)
    for key in _META_REQUIRED:
        if key not in raw:
            raise _DbDamaged(t("editstore.damaged.meta", key=key))
    meta: dict[str, Any] = {}
    try:
        for key, text in raw.items():
            if key in _META_JSON:
                meta[key] = _loads(text)
            elif key == "saving":
                meta[key] = _loads(text) if text else None
            elif key in _META_INT:
                meta[key] = int(text)
            elif key in _META_FLOAT:
                meta[key] = float(text)
            else:
                meta[key] = text
        meta["identity"] = FileIdentity.from_dict(meta["identity"])
        ends = meta.get("ends") or {}
        meta["ends"] = {int(k): int(v) for k, v in ends.items()}
        files = meta.get("files") or {}
        meta["files"] = {str(k): int(v) for k, v in files.items()}
        saving = meta.setdefault("saving", None)
        if saving is not None and not isinstance(saving, dict):
            raise ValueError("saving journal is not an object")
        meta.setdefault("record_path", None)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise _DbDamaged(t("editstore.damaged.meta_value", detail=str(exc))) from exc
    if meta["format_version"] > FORMAT_VERSION:
        err = EditError("editstore.error.format", version=meta["format_version"])
        raise _DbDamaged(err.message, err)
    return meta


def _open_db(db: Path, *, timeout: float) -> tuple[sqlite3.Connection, dict[str, Any]]:
    """Connect and read the meta. ``_DbLocked`` when another connection holds the database,
    ``_DbDamaged(reason)`` when it is not a readable layer."""
    try:
        con = _connect(db, timeout=timeout)
    except sqlite3.Error as exc:
        if isinstance(exc, sqlite3.OperationalError) and _is_busy(exc):
            raise _DbLocked() from exc
        raise _DbDamaged(t("editstore.damaged.db", detail=str(exc))) from exc
    try:
        rows = con.execute("SELECT key, value FROM meta").fetchall()
        return con, _parse_meta(rows)
    except sqlite3.Error as exc:
        con.close()
        if isinstance(exc, sqlite3.OperationalError) and _is_busy(exc):
            raise _DbLocked() from exc
        raise _DbDamaged(t("editstore.damaged.db", detail=str(exc))) from exc
    except BaseException:
        con.close()
        raise


def _meta_matches(meta: dict[str, Any], doc: Any, key: str, base: int) -> bool:
    mk = meta.get("key")
    if mk is not None and mk != key:
        return False
    return (doc.identity.matches(meta["identity"]) and meta.get("kind") == doc.info.kind
            and meta.get("record_path") == _record_path_json(doc.info.record_path)
            and meta.get("base_count") == base)


# ----------------------------------------------------------------------------------
# index.json (best effort registry of layers)
# ----------------------------------------------------------------------------------
def _index_file() -> Path:
    return _edits_root() / INDEX_NAME


def _index_load() -> list[dict[str, Any]]:
    try:
        path = _index_file()
        with open_shared(path, "rb") as f:
            raw = read_limited(f, _INDEX_MAX_BYTES)
    except (JsonReshapeError, OSError):
        return []
    if len(raw) > _INDEX_MAX_BYTES:
        log.warning("edit layer index %s is too large; ignored", path)
        return []
    try:
        data = _loads(raw)
        entries = data.get("layers") if isinstance(data, dict) else None
    except ValueError:
        log.warning("edit layer index %s is corrupt; ignored", path)
        return []
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("key"), str)
            and isinstance(e.get("dir"), str) and isinstance(e.get("source"), str)]


def _index_change(fn: Callable[[list[dict[str, Any]]], list[dict[str, Any]] | None]) -> None:
    """Read-modify-write ``index.json`` under its file lock; failures are logged (the index is a
    convenience: matching layers are found without it)."""
    try:
        from .query import file_lock  # lazy: query imports much of the package

        path = _index_file()
        with file_lock(path, busy_key="editstore.error.index_busy", timeout=INDEX_LOCK_TIMEOUT):
            entries = _index_load()
            new = fn(entries)
            if new is None:
                return
            with atomic_write(path) as f:
                f.write(_dumps({"version": INDEX_VERSION, "layers": new}).encode("utf-8"))
    except (JsonReshapeError, OSError) as exc:
        log.warning("cannot update the edit layer index: %s", exc)


def _index_put(entry: dict[str, Any]) -> None:
    def change(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = [e for e in entries if e.get("key") != entry["key"]
               and normalize_path(e.get("dir", "")) != normalize_path(entry["dir"])]
        out.append(entry)
        return out

    _index_change(change)


def _index_update(key: str, **fields_: Any) -> None:
    def change(entries: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        hit = False
        for e in entries:
            if e.get("key") == key:
                e.update(fields_)
                hit = True
        return entries if hit else None

    _index_change(change)


def _index_remove(key: str | None = None, dir_: Path | None = None) -> None:
    norm = None if dir_ is None else normalize_path(dir_)

    def change(entries: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        out = [e for e in entries if not ((key is not None and e.get("key") == key)
                                         or (norm is not None and normalize_path(e.get("dir", "")) == norm))]
        return out if len(out) != len(entries) else None

    _index_change(change)


def layer_sources() -> dict[str, list[dict[str, Any]]]:
    """The source files that have an edit layer, from ``index.json`` read ONCE: normalized source path
    (:func:`jsonreshape.fileio.normalize_path`, as the entries store it) -> copies of the index entries of its
    layers (``key``, ``dir``, ``display``, ``location``, ``created``, ``updated``, ``steps`` and, once a layer
    was closed with steps, ``label_key`` / ``label_params``), most recently updated first.

    Cheap (fix round L1): the small index is read with a buffer of its own size -- no lock taken, no layer
    opened -- so a caller that asks about many files (may these outputs be replaced?) reads it once and calls
    :func:`find_pending` only for the files listed here (and for a ``<file>.jredit`` folder that exists). Live
    layers (a tab or another instance edits the file) and pending ones (kept for recovery) are listed alike:
    the index cannot tell them apart, nor whether a layer still has steps -- :func:`find_pending` decides for
    one file. Only layers of the two standard locations are registered (a custom folder, e.g. the CLI's
    temporary one, never is); an entry whose layer was removed by a crash stays listed until
    :func:`find_pending` of that source drops it. A missing, unreadable, corrupt or too large index, or an
    unusable edit-layer folder: ``{}`` (best effort, like the index itself)."""
    try:
        path = _index_file()
        with open_shared(path, "rb") as f:
            if os.fstat(f.fileno()).st_size > _INDEX_MAX_BYTES:
                raw = None
            else:
                raw = f.read()            # (readall: a buffer of the file's size, not of the maximum)
    except (JsonReshapeError, OSError):
        return {}
    if raw is None or len(raw) > _INDEX_MAX_BYTES:
        log.warning("edit layer index %s is too large; ignored", path)
        return {}
    try:
        data = _loads(raw)
    except (ValueError, RecursionError):
        log.warning("edit layer index %s is corrupt; ignored", path)
        return {}
    entries = data.get("layers") if isinstance(data, dict) else None
    out: dict[str, list[dict[str, Any]]] = {}
    for e in entries if isinstance(entries, list) else ():
        if isinstance(e, dict) and isinstance(e.get("key"), str) and isinstance(e.get("dir"), str) \
                and isinstance(e.get("source"), str):
            out.setdefault(e["source"], []).append(dict(e))

    def updated(e: dict[str, Any]) -> float:
        value = e.get("updated")
        return -float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

    for items in out.values():
        items.sort(key=updated)
    return out


# ----------------------------------------------------------------------------------
# PendingInfo / find_pending
# ----------------------------------------------------------------------------------
@dataclass
class PendingInfo:
    """A layer with unsaved changes found for a document (§12.2)."""

    dir: Path
    key: str
    steps: int                  # head of the layer (steps in effect)
    created: float
    updated: float
    label: str                  # localized label of the last step
    matches: bool               # False: a layer of an OLDER version of this file
    locked: bool                # held by another tab/process right now -> information only
    damaged: str | None         # why the layer cannot be loaded -> only Delete / Export offered
    saving: dict | None         # an interrupted save journal (§12.7)
    source: str


class _Summary(NamedTuple):
    kind: str
    ts: float
    label_key: str
    label_params: dict
    struct: bool
    blob: bool


def _summary(step: Step) -> _Summary:
    return _Summary(step.kind, step.ts, step.label_key, step.label_params, step.struct is not None,
                    step.pieces_blob is not None)


def _light_check(path: Path, con: sqlite3.Connection, meta: dict[str, Any]) -> str | None:
    """Cheap damage check of a layer (no replay): segment sizes against the committed ends, layer
    files present, the step rows ``checkpoint+1 .. head`` present."""
    for seg, end in meta["ends"].items():
        size = _size(path / segment_name(seg))
        if size is None or size < end:
            return t("editstore.damaged.missing", name=segment_name(seg))
    for name in meta["files"]:
        if not _isfile(path / name):
            return t("editstore.damaged.missing", name=name)
    try:
        row = con.execute("SELECT seq FROM checkpoint WHERE id = 1").fetchone()
        cp = int(row[0]) if row else 0
        head = meta["head"]
        if head < cp:
            return t("editstore.damaged.steps")
        n = con.execute("SELECT COUNT(*) FROM steps WHERE seq > ? AND seq <= ?", (cp, head)).fetchone()[0]
        if n != head - cp:
            return t("editstore.damaged.steps")
    except sqlite3.Error as exc:
        return t("editstore.damaged.db", detail=str(exc))
    return None


def _head_label(con: sqlite3.Connection, head: int) -> str:
    if head < 1:
        return ""
    row = con.execute("SELECT data FROM steps WHERE seq = ?", (head,)).fetchone()
    if row is None:
        return ""
    try:
        label = _loads(row[0]).get("label")
        return step_label(label[0], label[1])
    except (ValueError, TypeError, AttributeError, IndexError):
        return t("editstore.step.unknown")


def delete_layer(dir: Path, *, key: str | None = None, lock: LayerLock | None = None) -> bool:  # noqa: A002
    """Delete the layer in ``dir`` (also a damaged one) while holding its lock: ``key`` (or the folder
    name of an app-folder layer, or the key in its meta) is locked first -- a ``lock`` the caller holds
    counts as taken; a layer in use raises ``EditError("edits.error.locked")``. True when nothing of it
    is left."""
    path = Path(dir)
    keys = {key} if key else set()
    if _KEY_RE.fullmatch(path.name):
        keys.add(path.name)
    db = path / DB_NAME
    if not keys and _isfile(db):
        try:
            con, meta = _open_db(db, timeout=0.0)
            con.close()
            if meta.get("key"):
                keys.add(meta["key"])
        except _DbLocked:
            raise EditError("edits.error.locked") from None
        except _DbDamaged:
            pass
    mine = lock.key if lock is not None and lock.held else None
    locks = []
    try:
        for k in sorted(keys):
            if k == mine:
                continue
            lk = LayerLock(k)
            if not lk.try_acquire():
                raise EditError("edits.error.locked")
            locks.append(lk)
        if _isfile(db):
            try:  # a live connection of another process holds the database exclusively
                con, _meta = _open_db(db, timeout=0.0)
                con.close()
            except _DbLocked:
                raise EditError("edits.error.locked") from None
            except _DbDamaged:
                pass
        ok = _purge_dir(path)
        _index_remove(key=key, dir_=path)
        for k in keys:
            _index_remove(key=k)
        return ok
    finally:
        for lk in locks:
            lk.release()


def find_pending(doc: Any, *, settings: Settings | None = None, lock: LayerLock | None = None
                 ) -> list[PendingInfo]:
    """Layers with unsaved changes for ``doc`` in both locations (§12.2): the matching layer first,
    then stale layers of the same normalized source path (``index.json``), most recent first. Only
    layers with head >= 1 (or an interrupted save journal); tombstoned and head-0 layers are purged
    silently when their lock is free. Locked layers are reported as information (``locked=True``).

    ``lock``: a LayerLock the caller holds (an editing session that took its document's lock before
    looking for pending layers): its key counts as free for the caller. Call it before the caller opens
    an :class:`EditStore` of that layer (an open store holds the database exclusively)."""
    key = layer_key(doc)
    mine = lock.key if lock is not None and lock.held else None
    _locks_root()                       # an unusable lock folder: editing is unavailable
    root = _edits_root()
    source_norm = normalize_path(doc.source_path)
    index = _index_load()
    cands: dict[str, tuple[Path, set[str]]] = {}

    def add(path: Path, keys: set[str]) -> None:
        norm = normalize_path(path)
        cur = cands.get(norm)
        if cur is None:
            cands[norm] = (path, set(keys))
        else:
            cur[1].update(keys)

    add(root / key, {key})
    bdir = beside_dir(doc)
    add(bdir, {key} | {e["key"] for e in index if normalize_path(e["dir"]) == normalize_path(bdir)})
    for e in index:
        if e.get("source") == source_norm and _KEY_RE.fullmatch(e["key"]):
            add(Path(e["dir"]), {e["key"]})
    out: list[PendingInfo] = []
    gone: list[dict[str, Any]] = []
    for path, keys in cands.values():
        info = _pending_info(doc, path, keys, key, index, mine)
        if info is not None:
            out.append(info)
    for e in index:
        if e.get("source") == source_norm and not _isfile(Path(e["dir"]) / DB_NAME):
            gone.append(e)
    if gone:
        dead = {e["key"] for e in gone}
        _index_change(lambda entries: [e for e in entries if e.get("key") not in dead]
                      if any(e.get("key") in dead for e in entries) else None)
    out.sort(key=lambda p: (not p.matches, -p.updated))
    return out


def _pending_info(doc: Any, path: Path, keys: set[str], key: str, index: list[dict[str, Any]],
                  mine: str | None) -> PendingInfo | None:
    db = path / DB_NAME
    if not _isfile(db):
        if _isdir(path) and _layer_file_names(_listdir(path)):
            _purge_unlocked(path, keys, mine)   # leftovers of a deleted layer
        return None
    locks: list[LayerLock] = []
    con: sqlite3.Connection | None = None
    try:
        for k in sorted(keys):
            if k == mine:
                continue
            lk = LayerLock(k)
            if not lk.try_acquire():
                return _locked_info(doc, path, k, key, index)
            locks.append(lk)
        try:
            con, meta = _open_db(db, timeout=0.0)
        except _DbLocked:
            return _locked_info(doc, path, path.name if path.name in keys else sorted(keys)[0], key, index)
        except _DbDamaged as exc:
            try:
                mtime = os.stat(long_path(db)).st_mtime
            except OSError:
                mtime = 0.0
            return PendingInfo(dir=path, key=path.name if path.name in keys else sorted(keys)[0], steps=0,
                               created=mtime, updated=mtime, label="", matches=path.name == key, locked=False,
                               damaged=exc.reason, saving=None, source=str(doc.source_path))
        mkey = meta.get("key") or (path.name if _KEY_RE.fullmatch(path.name) else key)
        if mkey not in keys and mkey != mine:
            lk = LayerLock(mkey)
            if not lk.try_acquire():
                return _locked_info(doc, path, mkey, key, index)
            locks.append(lk)
        if meta.get("state") == "discarded" or (meta["head"] < 1 and not meta.get("saving")):
            con.close()
            con = None
            _purge_dir(path)
            _index_remove(key=mkey)
            return None
        base = _base_count(doc)
        damaged = _light_check(path, con, meta)
        label = ""
        if damaged is None:
            try:
                label = _head_label(con, meta["head"])
            except sqlite3.Error as exc:
                damaged = t("editstore.damaged.db", detail=str(exc))
        return PendingInfo(dir=path, key=mkey, steps=meta["head"], created=meta["created"], updated=meta["updated"],
                           label=label, matches=_meta_matches(meta, doc, key, base), locked=False, damaged=damaged,
                           saving=meta.get("saving"), source=str(meta.get("source", doc.source_path)))
    finally:
        if con is not None:
            con.close()
        for lk in locks:
            lk.release()


def _purge_unlocked(path: Path, keys: set[str], mine: str | None = None) -> None:
    locks = []
    try:
        for k in sorted(keys):
            if k == mine:
                continue
            lk = LayerLock(k)
            if not lk.try_acquire():
                return
            locks.append(lk)
        if not _isfile(path / DB_NAME):
            _purge_dir(path)
    finally:
        for lk in locks:
            lk.release()


def _locked_info(doc: Any, path: Path, lock_key: str, key: str, index: list[dict[str, Any]]) -> PendingInfo:
    entry = next((e for e in index if normalize_path(e.get("dir", "")) == normalize_path(path)), None) or {}
    label = ""
    if entry.get("label_key"):
        label = step_label(entry["label_key"], entry.get("label_params") or {})
    ekey = entry.get("key") or lock_key
    return PendingInfo(dir=path, key=ekey, steps=int(entry.get("steps", 0) or 0),
                       created=float(entry.get("created", 0.0) or 0.0), updated=float(entry.get("updated", 0.0) or 0.0),
                       label=label, matches=ekey == key, locked=True, damaged=None, saving=None,
                       source=str(entry.get("display") or doc.source_path))


# ----------------------------------------------------------------------------------
# segment writers
# ----------------------------------------------------------------------------------
class _SegWriter:
    """The write handle of one segment. ``crc`` is the crc32 of the bytes ``[crc_from, pos)`` appended since the
    last commit that recorded them (a step's checksum of its new bytes)."""

    __slots__ = ("broken", "crc", "crc_from", "f", "path", "pos", "published", "seg")

    def __init__(self, seg: int, path: Path, f: Any, pos: int) -> None:
        self.seg = seg
        self.path = path
        self.f = f
        self.pos = pos
        self.published = pos
        self.broken = False
        self.crc = 0
        self.crc_from = pos


# ----------------------------------------------------------------------------------
# EditStore
# ----------------------------------------------------------------------------------
class EditStore:
    """The persistent edit layer of one document version (§12.2). Use :meth:`open`."""

    def __init__(self) -> None:
        raise TypeError("use EditStore.open(doc, ...)")

    # -- construction ----------------------------------------------------------------------
    @classmethod
    def _new(cls, *, key: str, base_count: int, dir: Path, location: str, lock: LayerLock, own_lock: bool,
             read_only: bool, doc_meta: dict[str, Any]) -> EditStore:
        self = cls.__new__(cls)
        self.key = key
        self.base_count = base_count
        self.dir = dir
        self.location = location
        self.read_only = read_only
        self.lock = lock
        self._own_lock = own_lock
        self._extra_locks: list[LayerLock] = []
        self.reader = DatReader(dir)
        self._own_reader = DatReader(dir)
        self.head = 0
        self._max_seq = 0
        self._cp_seq = 0
        self.created = False
        self.closed = False
        self._discarded = False
        self._con: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._writers: dict[int, _SegWriter] = {}
        self._published: dict[int, int] = {}
        self._point_seg = 0                     # the point-edit / blob segment (set when the layer is created)
        self._alloc_segs: set[int] = set()
        self._alloc_layers: set[str] = set()
        self._files: dict[str, int] = {}
        self._ends: dict[int, int] = {}
        self._layers: dict[str, ArrayLayer] = {}
        self._summ: dict[int, _Summary] = {}
        self._cp_state: OverlayState | None = None
        self._cp_blobs: list[tuple[Any, list[int]]] = []
        self._cp_crcs: list[tuple[int, int, int, int]] = []    # checksums the checkpoint row holds
        self._mirror: tuple[int, PieceTable] | None = None
        self._next_rid_hw = base_count
        self._doc_meta = doc_meta
        self._meta: dict[str, Any] = {}
        self.warnings: list[tuple[str, dict[str, Any]]] = []
        self.saving: dict | None = None
        self.data_size = int(doc_meta.get("data_size", 0))
        self.created_at = 0.0
        self.stale = False
        return self

    @classmethod
    def open(cls, doc: Any, *, settings: Settings | None = None, create: bool = True, dir: Path | None = None,  # noqa: A002
             lock: LayerLock | None = None, read_only: bool = False, allow_stale: bool = False) -> EditStore | None:
        """Open the layer of ``doc`` (§12.2). ``create=False`` (or ``read_only``) and no layer -> None.

        Takes ``lock`` (the held LayerLock of the document's key) or acquires one itself (released at
        close). A matching layer in either location is opened; otherwise a new layer is planned in the
        configured location (``dir`` overrides it) and created by the first write. A layer of an older
        version of the file occupying the planned folder raises ``EditError("editstore.error.stale")``
        unless ``allow_stale`` (read-only export of an old layer: open with ``dir=`` and
        ``read_only=True``)."""
        s = settings if settings is not None else get_settings()
        key = layer_key(doc)
        base = _base_count(doc)
        own = lock is None
        if own:
            lock = LayerLock(key)
            lock.acquire()
        elif not isinstance(lock, LayerLock) or lock.key != key or not lock.held:
            raise ValueError("lock must be the held LayerLock of this document's layer")
        assert lock is not None
        extra: list[LayerLock] = []
        conns: list[sqlite3.Connection] = []
        try:
            if dir is not None:
                cands = [(Path(dir), _location_of(Path(dir), doc))]
            else:
                app = (_edits_root() / key, "appdata")
                bes = (beside_dir(doc), "beside")
                cands = [bes, app] if s.edits_location == "beside" else [app, bes]
            planned = cands[0]
            chosen: tuple[Path, str, sqlite3.Connection, dict[str, Any]] | None = None
            occupant: tuple[Path, str, sqlite3.Connection, dict[str, Any]] | None = None
            for path, location in cands:
                db = path / DB_NAME
                if not _isfile(db):
                    continue
                ours = location == "appdata" or (path, location) == planned
                try:
                    con, meta = _open_db(db, timeout=_DB_TIMEOUT if location == "appdata" else 0.0)
                except _DbLocked:
                    if ours:
                        raise EditError("edits.error.locked") from None
                    continue
                except _DbDamaged as exc:
                    if ours:
                        if exc.error is not None:
                            raise exc.error from None
                        raise _damaged(0, exc.reason, last_good=None) from None
                    continue
                conns.append(con)
                mkey = meta.get("key")
                if meta.get("state") == "discarded":
                    con.close()
                    conns.remove(con)
                    if not read_only:
                        cls._purge_other(path, mkey, key)
                    continue
                if _meta_matches(meta, doc, key, base):
                    chosen = (path, location, con, meta)
                    break
                if (path, location) == planned:
                    occupant = (path, location, con, meta)
            stale = False
            if chosen is None and occupant is not None:
                if not allow_stale:
                    raise EditError("editstore.error.stale", path=str(occupant[0]))
                mkey = occupant[3].get("key")
                if mkey and mkey != key:
                    lk = LayerLock(mkey)
                    if not lk.try_acquire():
                        raise EditError("edits.error.locked")
                    extra.append(lk)
                chosen = occupant
                stale = True
            doc_meta = {
                "source": str(doc.source_path),
                "source_norm": normalize_path(doc.source_path),
                "identity": doc.identity.to_dict(),
                "kind": doc.info.kind,
                "record_path": _record_path_json(doc.info.record_path),
                "base_count": base,
                "data_size": int(getattr(doc, "data_size", 0) or 0),
            }
            if chosen is None:
                for c in conns:
                    c.close()
                conns.clear()
                if read_only or not create:
                    if own:
                        lock.release()
                    return None
                path, location = planned
                return cls._new(key=key, base_count=base, dir=path, location=location, lock=lock, own_lock=own,
                                read_only=False, doc_meta=doc_meta)
            path, location, con, meta = chosen
            for c in conns:
                if c is not con:
                    c.close()
            conns = [con]
            store = cls._new(key=meta.get("key") or key, base_count=int(meta["base_count"]), dir=path,
                             location=location, lock=lock, own_lock=own, read_only=read_only or stale,
                             doc_meta=doc_meta)
            store.stale = stale
            store._extra_locks = extra
            store._attach(con, meta)
            conns.clear()
            return store
        except BaseException:
            for c in conns:
                c.close()
            for lk in extra:
                lk.release()
            if own:
                lock.release()
            raise

    @staticmethod
    def _purge_other(path: Path, mkey: str | None, key: str) -> None:
        """Purge a tombstoned layer folder (its own lock taken when it is another key's)."""
        lk = None
        if mkey and mkey != key:
            lk = LayerLock(mkey)
            if not lk.try_acquire():
                return
        try:
            _purge_dir(path)
            _index_remove(key=mkey or key)
        finally:
            if lk is not None:
                lk.release()

    def _attach(self, con: sqlite3.Connection, meta: dict[str, Any]) -> None:
        """Adopt an existing layer: read its counters, publish the committed ends, clean up."""
        self._con = con
        self.created = True
        self._meta = meta
        self.head = int(meta["head"])
        self._next_rid_hw = max(self.base_count, int(meta["next_rid"]))
        self.saving = meta.get("saving")
        self._files = dict(meta["files"])
        self._ends = dict(meta["ends"])
        self.data_size = int(meta.get("data_size", self.data_size) or 0)
        self.created_at = float(meta["created"])
        try:
            row = con.execute("SELECT seq FROM checkpoint WHERE id = 1").fetchone()
            self._cp_seq = int(row[0]) if row else 0
            row = con.execute("SELECT MAX(seq) FROM steps").fetchone()
            self._max_seq = max(self._cp_seq, int(row[0]) if row and row[0] is not None else 0)
        except sqlite3.Error as exc:
            raise _damaged(0, f"database: {exc}", last_good=None) from exc
        self._point_seg = int(meta.get("point_seg", 0) or 0)
        names = _listdir(self.dir)
        seg_floor, layer_floor = _numbers_in([*names, *self._files])
        seg_floor = max(seg_floor, self._point_seg + 1, max([-1, *self._ends]) + 1)
        _claim_numbers(self.dir, seg_floor, layer_floor)
        for seg, end in self._ends.items():
            self._publish(seg, end)
        if not self.read_only:
            self._gc_files(names)

    @property
    def point_segment(self) -> int:
        """The segment of point edits and blobs (``seg-00000.dat`` unless this process used the folder for
        another layer before: segment and layer-file names are never reused within a process)."""
        return self._point_seg

    def _gc_files(self, names: list[str]) -> None:
        """Delete files no committed step references and cut segment tails beyond the committed ends
        (the store holds the layer lock). Files whose deletion waits for alive states are left alone."""
        for name in names:
            if _is_retained(self.dir / name):
                continue
            m = _SEG_FILE.fullmatch(name)
            if m:
                seg = int(m.group(1))
                if seg != self._point_seg and name not in self._files and seg not in self._ends:
                    _remove(self.dir / name)
                    continue
                end = self._ends.get(seg, 0)
                size = _size(self.dir / name)
                if size is not None and size > end:
                    try:
                        with open_shared(self.dir / name, "r+b", buffering=0) as f:
                            f.truncate(end)
                    except (JsonReshapeError, OSError):
                        log.warning("cannot truncate %s to its committed end", self.dir / name, exc_info=True)
                continue
            if _LAYER_FILE.fullmatch(name):
                if name not in self._files:
                    _remove(self.dir / name)
                continue
            if _TEMP_FILE.fullmatch(name):
                _remove(self.dir / name)

    # -- basics ------------------------------------------------------------------------------
    def __repr__(self) -> str:
        return (f"<EditStore {self.key} {self.location} head={self.head} created={self.created} "
                f"closed={self.closed}>")

    def __enter__(self) -> EditStore:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def checkpoint_seq(self) -> int:
        """Oldest seq the history can go back to (undo floor)."""
        return self._cp_seq

    @property
    def max_seq(self) -> int:
        """Last stored step (``> head`` when redo is possible)."""
        return self._max_seq

    def _check_open(self) -> None:
        if self.closed:
            raise EditError("editstore.error.closed")

    def _check_writable(self) -> None:
        self._check_open()
        if self.read_only:
            raise EditError("editstore.error.read_only")
        if self._discarded:
            raise EditError("editstore.error.discarded")

    def _publish(self, seg: int, end: int) -> None:
        self._published[seg] = end
        self._own_reader.publish(seg, end)
        self.reader.publish(seg, end)

    def published(self, seg: int) -> int:
        """Published (synced) size of segment ``seg``."""
        return self._published.get(seg, 0)

    def segment_path(self, seg: int) -> Path:
        return self.dir / segment_name(seg)

    # -- creation ------------------------------------------------------------------------------
    def _ensure_created(self) -> None:
        if self.created:
            return
        self._check_writable()
        tries = [(self.dir, self.location)]
        if self.location == "beside":
            tries.append((_edits_root() / self.key, "appdata"))
        last: BaseException | None = None
        for path, location in tries:
            try:
                self._create_at(path)
            except EditError:
                raise
            except (OSError, sqlite3.Error, JsonReshapeError) as exc:
                last = exc
                if location == "beside":
                    continue
                raise _write_error(exc, path) from exc
            if path != self.dir:
                assert last is not None
                notice = _note_fallback(self.dir, path, _reason(last, self.dir))
                self.warnings.append(notice)
                self.dir = path
                self.location = location
                self.reader.dir = path         # no segment handle exists yet
                self._own_reader.dir = path
            break
        if self.location in ("appdata", "beside"):
            _index_put({"key": self.key, "dir": str(self.dir), "source": self._doc_meta["source_norm"],
                        "display": self._doc_meta["source"], "created": self.created_at, "updated": self.created_at,
                        "steps": 0, "location": self.location})

    def _create_at(self, path: Path) -> None:
        made = not _exists(path)
        make_dirs(path)
        if _isfile(path / DB_NAME):
            raise EditError("editstore.error.stale", path=str(path))
        for name in _layer_file_names(_listdir(path)):   # garbage of a deleted layer (lock held)
            if not _is_retained(path / name):            # (files alive states still read are left alone)
                _remove(path / name)
        _claim_numbers(path, *_numbers_in(_listdir(path)))
        point = _take_number(path, 0)                    # 0 unless this process used the folder before
        con = None
        try:
            con = _connect(path / DB_NAME, timeout=_DB_TIMEOUT)
            now = time.time()
            meta = dict(self._doc_meta)
            meta.update(format_version=FORMAT_VERSION, key=self.key, created=now, updated=now, head=0,
                        next_rid=self.base_count, state="live", saving=None, app_version=__version__, ends={},
                        files={}, point_seg=point)
            con.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _SCHEMA:
                    con.execute(stmt)
                con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                [(k, _meta_text(k, v)) for k, v in meta.items()])
                con.execute("COMMIT")
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except BaseException:
            if con is not None:
                con.close()
            for name in _DB_FILES:
                _remove(path / name)
            if made:
                try:
                    os.rmdir(long_path(path))
                except OSError:
                    pass
            raise
        self._con = con
        self.created = True
        self.created_at = now
        self._meta = meta
        self._point_seg = point

    # -- transactions ------------------------------------------------------------------------
    def _txn(self, fn: Callable[[sqlite3.Connection], None], point: str) -> None:
        con = self._con
        assert con is not None
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                fn(con)
                _crash_point(point)
                con.execute("COMMIT")
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.Error as exc:
            raise _write_error(exc, self.dir / DB_NAME) from exc

    @staticmethod
    def _set_meta(con: sqlite3.Connection, **values: Any) -> None:
        con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                        [(k, _meta_text(k, v)) for k, v in values.items()])

    # -- segments ------------------------------------------------------------------------------
    def _writer(self, seg: int) -> _SegWriter:
        w = self._writers.get(seg)
        if w is not None:
            return w
        if seg != self._point_seg and seg not in self._alloc_segs:
            raise ValueError(f"segment {seg} was not created by this store")
        path = self.segment_path(seg)
        try:
            f = open_shared(path, "r+b" if _isfile(path) else "w+b", buffering=0)
        except JsonReshapeError as exc:
            raise _write_error(exc, path) from exc
        pos = self._published.get(seg, 0)
        try:
            if os.fstat(f.fileno()).st_size != pos:
                f.truncate(pos)
        except OSError as exc:
            f.close()
            raise _write_error(exc, path) from exc
        w = _SegWriter(seg, path, f, pos)
        self._writers[seg] = w
        return w

    def append(self, data: bytes | bytearray | memoryview, *, seg: int | None = None) -> tuple[int, int]:
        """Append ``data`` to segment ``seg`` (``None``: the point-edit segment, :attr:`point_segment`);
        returns its virtual span (readable after :meth:`sync`).

        Unbuffered writes at the store's write position (the committed end after an open). On an
        OS error the segment is truncated back and ``EditError("editstore.error.disk_full")`` /
        ``("editstore.error.write")`` is raised; the earlier bytes stay intact."""
        with self._lock:
            self._check_writable()
            self._ensure_created()
            w = self._writer(self._point_seg if seg is None else seg)
            if w.broken:
                raise EditError("editstore.error.write", path=str(w.path), detail="segment unusable after an error")
            view = memoryview(data).cast("B")
            n = len(view)
            start = w.pos
            if start + n >= SEG_SIZE_LIMIT:
                raise EditError("editstore.error.write", path=str(w.path), detail="segment size limit reached")
            if n:
                try:
                    w.f.seek(start)
                    off = 0
                    while off < n:
                        k = _raw_write(w.f, view[off:])
                        if k is None or k <= 0:
                            raise OSError(_errno.EIO, "short write", str(w.path))
                        off += k
                except OSError as exc:
                    try:
                        w.f.truncate(start)
                    except OSError:
                        w.broken = True
                        log.error("segment %s could not be truncated after a failed write", w.path)
                    raise _write_error(exc, w.path) from exc
                w.crc = zlib.crc32(view, w.crc)
            w.pos = start + n
            _crash_point("append")
            vstart = edit_offset(w.seg, start)
            return vstart, vstart + n

    def sync(self, segs: Iterable[int] | None = None) -> None:
        """fsync the given segments (default: every open one) and publish their ends."""
        with self._lock:
            self._check_open()
            targets = list(self._writers) if segs is None else list(segs)
            for seg in targets:
                w = self._writers.get(seg)
                if w is None or w.pos <= w.published:
                    continue
                try:
                    os.fsync(w.f.fileno())
                except OSError as exc:
                    raise _write_error(exc, w.path) from exc
                w.published = w.pos
                self._publish(seg, w.pos)

    def new_segment(self) -> int:
        """A new empty segment (``seg-<n>.dat``) for a preview / big patch; numbers are never reused (in this
        process not even by another layer in the same folder)."""
        with self._lock:
            self._check_writable()
            self._ensure_created()
            seg = _take_number(self.dir, 0)
            self._alloc_segs.add(seg)
            self._writer(seg)
            return seg

    def drop_segment(self, seg: int) -> None:
        """Delete a segment no committed step uses (a discarded preview)."""
        with self._lock:
            self._check_open()
            seg = _uint(seg, "segment")
            if seg == self._point_seg or segment_name(seg) in self._files:
                raise ValueError(f"segment {seg} is used by the layer")
            w = self._writers.pop(seg, None)
            if w is not None:
                w.f.close()
            self._published.pop(seg, None)
            self._own_reader.forget(seg)
            self.reader.forget(seg)
            self._alloc_segs.discard(seg)
            if not _is_retained(self.segment_path(seg)):
                _remove(self.segment_path(seg))

    def new_layer_path(self) -> Path:
        """A fresh ``layer-<n>.bin`` path in the layer folder (for ``write_array_layer``)."""
        with self._lock:
            self._check_writable()
            self._ensure_created()
            n = _take_number(self.dir, 1)
            name = f"layer-{n:05d}.bin"
            self._alloc_layers.add(name)
            return self.dir / name

    def drop_layer_file(self, path: Path | str) -> None:
        """Delete an array-layer file no committed step uses (a discarded preview)."""
        with self._lock:
            self._check_open()
            name = Path(path).name
            if name in self._files:
                raise ValueError(f"layer file {name} is used by the layer")
            self._layers.pop(name, None)
            self._alloc_layers.discard(name)
            _remove(self.dir / name)

    def layer(self, name: str) -> ArrayLayer:
        """The array layer file ``name`` of this layer folder (opened once and cached; reopened when a
        Document closed it)."""
        with self._lock:
            cur = self._layers.get(name)
            if cur is not None and not cur.closed:
                return cur
            if Path(name).name != name or not _LAYER_FILE.fullmatch(name):
                raise ValueError(f"invalid array layer name {name!r}")
            layer = ArrayLayer.open(self.dir / name)
            self._layers[name] = layer
            return layer

    def _blob_writer(self, data: bytes) -> list[int]:
        s, e = self.append(data)
        return [s, e]

    def _read_blob(self, span: Sequence[int]) -> bytes:
        return self._own_reader.read(int(span[0]), int(span[1]))

    # -- summaries / pieces --------------------------------------------------------------------
    def _summary_of(self, seq: int) -> _Summary:
        s = self._summ.get(seq)
        if s is None:
            self.step(seq)
            s = self._summ[seq]
        return s

    def _struct_since_blob(self) -> int:
        """Structural steps after the last piece-table snapshot (or the checkpoint) up to head."""
        n = 0
        for seq in range(self.head, self._cp_seq, -1):
            s = self._summary_of(seq)
            if s.blob:
                break
            if s.struct:
                n += 1
        return n

    def _pieces_at(self, seq: int) -> PieceTable:
        """The piece table at ``seq`` (checkpoint, nearest snapshot, structural replay)."""
        if self._mirror is not None and self._mirror[0] == seq:
            return self._mirror[1]
        steps = [self.step(s) for s in range(self._cp_seq + 1, seq + 1)]
        pieces: PieceTable | None = None
        start = 0
        for i in range(len(steps) - 1, -1, -1):
            if steps[i].pieces_blob is not None:
                pieces = PieceTable.from_bytes(self._read_blob(steps[i].pieces_blob))
                start = i + 1
                break
        if pieces is None:
            pieces = self._checkpoint_state().pieces
        for st in steps[start:]:
            if st.remap is not None:
                pieces = pieces.apply(st.remap, new_rids=st.new_rids)
        self._mirror = (seq, pieces)
        return pieces

    # -- commit / head ---------------------------------------------------------------------------
    def commit(self, step: Step, *, state: OverlayState | None = None) -> int:
        """Persist ``step`` as ``seq = head + 1`` and return the seq (§12.2).

        Syncs the segments the step depends on, then ONE transaction deletes the steps ``> head``
        (their private segments/layer files are deleted afterwards -- when no alive state of a document
        reads them any more), inserts the step with the checksums of the bytes it appended and updates the
        meta (head, updated, next_rid, ends, files). ``state`` (the state at head, before the step)
        lets the store fill ``old_rids`` and the 64th-step piece-table snapshot without replaying its
        history. Sets ``step.seq``/``step.ts`` (and ``old_rids``/``pieces_blob`` when it fills them)."""
        if not isinstance(step, Step):
            raise TypeError("commit() takes a Step")
        with self._lock:
            self._check_writable()
            self._ensure_created()
            head = self.head
            seq = head + 1
            if state is not None and (state.version != head or state.base_count != self.base_count):
                raise ValueError(f"state (version {state.version}) is not the head ({head}) of this layer")
            for sg in step.segments:
                if sg not in self._alloc_segs:
                    raise ValueError(f"step segment {sg} was not created by this store")
            layer = None
            if step.push_layer is not None:
                if step.push_layer in self._files:
                    raise ValueError(f"array layer {step.push_layer} is already used")
                layer = self.layer(step.push_layer)
                for sg in layer.segments:
                    if sg != self._point_seg and sg not in self._published and sg not in self._alloc_segs:
                        raise ValueError(f"array layer {step.push_layer} references the unknown segment {sg}")
            pre: PieceTable | None = None
            post: PieceTable | None = None
            remap = step.remap
            if remap is not None:
                if step.old_rids is None:
                    pre = state.pieces if state is not None else self._pieces_at(head)
                    step.fill_old_rids(pre)
                if step.pieces_blob is None and self._struct_since_blob() + 1 >= PIECES_BLOB_EVERY:
                    if pre is None:
                        pre = state.pieces if state is not None else self._pieces_at(head)
                    post = pre.apply(remap, new_rids=step.new_rids)
                    step.pieces_blob = self.append(post.to_bytes())
            deps = self._deps(step, layer)
            for sg in sorted(deps):        # a new segment the step uses belongs to it
                if sg != self._point_seg and segment_name(sg) not in self._files and sg not in step.segments:
                    if sg not in self._alloc_segs:
                        raise ValueError(f"step references the unknown segment {sg}")
                    step.segments.append(sg)
            data = step.to_json(self._blob_writer)
            deps = self._deps(step, layer)
            self.sync(deps)
            ends = {sg: self._published.get(sg, 0) for sg in sorted(deps)}
            self._check_spans(step, ends)
            data["ends"] = {str(k): v for k, v in ends.items()}
            crcs = self._new_crcs(deps)
            if crcs:
                data["crc"] = crcs
            text = _dumps(data)
            ts = time.time()
            truncated = sorted(name for name, owner in self._files.items() if owner > head)
            files = {name: owner for name, owner in self._files.items() if owner <= head}
            for sg in step.segments:
                files[segment_name(sg)] = seq
            if step.push_layer is not None:
                files[step.push_layer] = seq
            new_ends = {sg: e for sg, e in self._ends.items()
                        if segment_name(sg) not in truncated or sg == self._point_seg}
            for sg, e in ends.items():
                if e > new_ends.get(sg, -1):
                    new_ends[sg] = e
            hw = max(self._next_rid_hw, step_next_rid(step), state.next_rid if state is not None else 0)

            def write(con: sqlite3.Connection) -> None:
                con.execute("DELETE FROM steps WHERE seq > ?", (head,))
                con.execute("INSERT INTO steps(seq, ts, kind, data) VALUES (?, ?, ?, ?)", (seq, ts, step.kind, text))
                self._set_meta(con, head=seq, updated=ts, next_rid=hw, ends=new_ends, files=files)

            self._txn(write, "commit")
            _crash_point("commit.done")
            self.head = seq
            self._max_seq = seq
            self._files = files
            self._ends = new_ends
            self._next_rid_hw = hw
            for s in [s for s in self._summ if s > head]:
                del self._summ[s]
            step.seq = seq
            step.ts = ts
            self._summ[seq] = _summary(step)
            if post is not None:
                self._mirror = (seq, post)
            elif remap is None and self._mirror is not None and self._mirror[0] == head:
                self._mirror = (seq, self._mirror[1])       # the structure did not change
            else:
                self._mirror = None
            self._alloc_segs.difference_update(step.segments)
            if step.push_layer is not None:
                self._alloc_layers.discard(step.push_layer)
            self._crcs_recorded(deps)
            if truncated:
                self._retire_files(truncated, above=head)
            return seq

    def _new_crcs(self, segs: Iterable[int]) -> list[list[int]]:
        """``[seg, start, end, crc32]`` of the bytes appended to ``segs`` since they were last recorded (all
        synced: ``sync`` ran on them)."""
        out = []
        for sg in sorted(segs):
            w = self._writers.get(sg)
            if w is not None and w.pos > w.crc_from and w.published == w.pos:
                out.append([sg, w.crc_from, w.pos, w.crc])
        return out

    def _crcs_recorded(self, segs: Iterable[int]) -> None:
        for sg in segs:
            w = self._writers.get(sg)
            if w is not None and w.published == w.pos:
                w.crc_from = w.pos
                w.crc = 0

    def _retire_files(self, names: Sequence[str], *, above: int | None) -> None:
        """The private files ``names`` no step uses any more (truncated redo steps): their writers are closed
        and their cached layers dropped; the files are deleted now, or when the alive states that may still
        read them (``version > above``) are gone (deferred deletion, see the module section)."""
        for name in names:
            m = _SEG_FILE.fullmatch(name)
            if m:
                seg = int(m.group(1))
                w = self._writers.pop(seg, None)
                if w is not None:
                    w.f.close()
                self._published.pop(seg, None)
                self._own_reader.forget(seg)     # the store's private reader never reads it again
            else:
                self._layers.pop(name, None)     # closed with the file when nothing reads it
        _defer_delete(self.dir, names, _witnesses(self.dir, above=above))

    def _deps(self, step: Step, layer: ArrayLayer | None) -> set[int]:
        deps: set[int] = set(step.segments)
        pt = self._point_seg
        w0 = self._writers.get(pt)
        if self._published.get(pt, 0) or (w0 is not None and w0.pos):
            deps.add(pt)
        if layer is not None:
            deps.update(layer.segments)
        spans: list[tuple[int, int] | None] = []
        for _rid, old, new in step.content:
            spans.append(old)
            spans.append(new)
        if step.wrapper is not None:
            spans.extend(step.wrapper)
        spans.append(step.pieces_blob)
        for sp in spans:
            if sp is not None and sp[0] >= EDIT_BASE:
                deps.add(split_edit_offset(sp[0])[0])
        return deps

    def _check_spans(self, step: Step, ends: Mapping[int, int]) -> None:
        """Every span of a new step lies inside the synced bytes (catches callers that reference bytes
        they did not append to this layer)."""
        for _rid, _old, new in step.content:
            if new is not None and not _span_ok(new, ends, self.data_size):
                raise ValueError(f"step content span {new} is outside the layer's synced bytes")
        if step.wrapper is not None:
            for sp in (step.wrapper[1], step.wrapper[3]):
                if sp is not None and not _span_ok(sp, ends, self.data_size):
                    raise ValueError(f"step wrapper span {sp} is outside the layer's synced bytes")

    def set_head(self, seq: int) -> None:
        """Move the head (undo/redo); durable before it returns."""
        with self._lock:
            self._check_writable()
            seq = _uint(seq, "seq")
            if not self._cp_seq <= seq <= self._max_seq:
                raise ValueError(f"head {seq} outside {self._cp_seq}..{self._max_seq}")
            if seq == self.head:
                return
            now = time.time()
            self._txn(lambda con: self._set_meta(con, head=seq, updated=now), "set_head")
            self.head = seq
            if self._mirror is not None and self._mirror[0] != seq:
                self._mirror = None

    def can_redo(self) -> bool:
        with self._lock:
            return self._max_seq > self.head

    def step(self, seq: int) -> Step:
        """The stored step ``seq`` (``checkpoint_seq < seq <= max_seq``)."""
        with self._lock:
            self._check_open()
            seq = _uint(seq, "seq")
            if not self.created or not self._cp_seq < seq <= self._max_seq:
                raise ValueError(f"no step {seq} (steps {self._cp_seq + 1}..{self._max_seq})")
            assert self._con is not None
            try:
                row = self._con.execute("SELECT ts, data FROM steps WHERE seq = ?", (seq,)).fetchone()
            except sqlite3.Error as exc:
                raise _damaged(seq, f"database: {exc}") from exc
            if row is None:
                raise _damaged(seq, "step row missing")
            try:
                st = Step.from_json(_loads(row[1]), self._own_reader.read, seq=seq, ts=row[0])
            except (ValueError, TypeError) as exc:
                raise _damaged(seq, f"step data: {exc}") from exc
            except EditError as exc:
                raise _damaged(seq, f"step blob: {exc.params.get('detail') or exc.key}") from exc
            self._summ[seq] = _summary(st)
            return st

    def steps(self) -> list[StepInfo]:
        """Every stored step (``checkpoint_seq+1 .. max_seq``, also redo ones) with its localized label."""
        with self._lock:
            self._check_open()
            out = []
            for seq in range(self._cp_seq + 1, self._max_seq + 1):
                s = self._summary_of(seq)
                out.append(StepInfo(seq, s.ts, s.kind, step_label(s.label_key, s.label_params)))
            return out

    # -- loading ---------------------------------------------------------------------------------
    def _checkpoint_state(self) -> OverlayState:
        if self._cp_state is not None:
            return self._cp_state
        state = self._load_checkpoint()
        self._cp_state = state
        return state

    def _load_checkpoint(self) -> OverlayState:
        if not self.created or self._cp_seq == 0:
            return OverlayState.initial(self.base_count, self.reader)
        assert self._con is not None
        cp = self._cp_seq
        try:
            row = self._con.execute("SELECT seq, data FROM checkpoint WHERE id = 1").fetchone()
        except sqlite3.Error as exc:
            raise _damaged(cp, f"checkpoint: {exc}", last_good=None) from exc
        if row is None or int(row[0]) != cp:
            raise _damaged(cp, "checkpoint row missing", last_good=None)
        try:
            data = _loads(row[1])
            ends = {int(k): int(v) for k, v in (data.get("ends") or {}).items()}
            self._check_ends(ends, cp, last_good=None)
            crcs = _crc_entries(data.get("crc"))
            self._verify_crcs(crcs, cp, last_good=None)
            pj = data["pieces"]
            if "identity" in pj:
                pieces = PieceTable.identity(_uint(pj["identity"], "checkpoint"))
                pspan: list[int] | None = None
            else:
                pspan = [int(pj["blob"][0]), int(pj["blob"][1])]
                pieces = PieceTable.from_bytes(self._read_blob(pspan))
            layers: list[Any] = []
            blobs: list[tuple[Any, list[int]]] = []
            if pspan is not None:
                blobs.append((pieces, pspan))
            for item in data["layers"]:
                if "dict" in item:
                    if item["dict"] is None:
                        layers.append(DictLayer())
                        continue
                    span = [int(item["dict"][0]), int(item["dict"][1])]
                    dl = DictLayer.from_bytes(self._read_blob(span))
                    for _rid, sp in dl.items():
                        if not _span_ok(sp, ends, self.data_size):
                            raise ValueError(f"checkpoint span {sp} outside the committed bytes")
                    layers.append(dl)
                    blobs.append((dl, span))
                else:
                    layers.append(self.layer(item["array"]))
            state = OverlayState(base_count=self.base_count, pieces=pieces, layers=tuple(layers),
                                 wrapper=data.get("wrapper"), wrapper_hole=data.get("hole"),
                                 flags=int(data.get("flags", 0)), next_rid=int(data.get("next_rid", 0)), version=cp,
                                 reader=self.reader)
        except LayerDamagedError:
            raise
        except (ValueError, TypeError, KeyError, EditError) as exc:
            raise _damaged(cp, f"checkpoint: {exc}", last_good=None) from exc
        self._cp_blobs = blobs
        self._cp_crcs = crcs
        return state

    def _check_ends(self, ends: Mapping[int, int], seq: int, *, last_good: int | None | str = "auto",
                    sizes: dict[int, int | None] | None = None) -> None:
        for sg, end in ends.items():
            if sizes is not None and sg in sizes:
                size = sizes[sg]
            else:
                size = _size(self.segment_path(sg))
                if sizes is not None:
                    sizes[sg] = size
            if size is None or size < end:
                raise _damaged(seq, f"{segment_name(sg)} is missing or shorter than {end} bytes",
                               last_good=last_good)

    def _crc_extend(self, sg: int, start: int, end: int, crc: int = 0, cancel: CancelToken | None = None) -> int:
        """``crc`` continued over the committed bytes ``[start, end)`` of segment ``sg`` (read in 1 MiB pieces)."""
        off = start
        while off < end:
            check(cancel)
            n = min(_CRC_READ, end - off)
            crc = zlib.crc32(self._own_reader.read(edit_offset(sg, off), edit_offset(sg, off + n)), crc)
            off += n
        return crc

    def _crc_range(self, sg: int, start: int, end: int, cancel: CancelToken | None = None) -> int:
        """crc32 of the committed bytes ``[start, end)`` of segment ``sg``. A range of at least
        :data:`_CRC_PARALLEL_MIN` bytes is cut into up to :data:`_CRC_THREADS` pieces read and checksummed by as
        many threads at once (the reader keeps one handle per thread; ``zlib.crc32`` releases the GIL) and the
        pieces' checksums are joined with :func:`crc32_combine`: the same value, the reads overlap (a restore of
        a bulk layer as big as the file reads all of it)."""
        threads = min(_CRC_THREADS, os.cpu_count() or 1)
        if end - start < _CRC_PARALLEL_MIN or threads <= 1:
            return self._crc_extend(sg, start, end, 0, cancel)
        step = -(-(end - start) // threads)
        step += -step % _CRC_READ
        cuts = [*range(start, end, step), end]
        parts = list(zip(cuts[:-1], cuts[1:]))
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=len(parts), thread_name_prefix="jsonreshape-crc") as ex:
            futures = [ex.submit(self._crc_extend, sg, a, b, 0, cancel) for a, b in parts]
            crcs = [f.result() for f in futures]
        crc = crcs[0]
        for c, (a, b) in zip(crcs[1:], parts[1:]):
            crc = crc32_combine(crc, c, b - a)
        return crc

    def _verify_crcs(self, entries: Sequence[tuple[int, int, int, int]], seq: int, *,
                     last_good: int | None | str = "auto", cancel: CancelToken | None = None) -> None:
        """The bytes of every checksum entry are unchanged, else :class:`LayerDamagedError` at ``seq``
        (rows written before checksums existed have none: nothing to check)."""
        for sg, s, e, c in entries:
            try:
                calc = self._crc_range(sg, s, e, cancel)
            except EditError as exc:
                raise _damaged(seq, f"{segment_name(sg)} bytes {s}..{e}: {exc.params.get('detail') or exc.key}",
                               last_good=last_good) from exc
            if calc != c:
                raise _damaged(seq, f"{segment_name(sg)} bytes {s}..{e} do not match their checksum",
                               last_good=last_good)

    def _merge_crcs(self, entries: Iterable[tuple[int, int, int, int]], read_below: int = 0
                    ) -> list[tuple[int, int, int, int]]:
        """Checksum entries merged per segment (``crc32_combine``). A gap in the point segment that ends at or
        below ``read_below`` -- bytes of steps truncated as redo, which no row covers -- is read and included,
        so the folded history stays one entry; gaps above it hold bytes of steps that are still rows (they
        have their own entries) and are left apart."""
        by_seg: dict[int, list[tuple[int, int, int]]] = {}
        for sg, s, e, c in entries:
            if e > s:
                by_seg.setdefault(sg, []).append((s, e, c))
        out: list[tuple[int, int, int, int]] = []
        for sg in sorted(by_seg):
            ranges = sorted(by_seg[sg])
            cs, ce, cc = ranges[0]
            for s, e, c in ranges[1:]:
                if ce < s <= read_below and sg == self._point_seg:
                    try:
                        cc = self._crc_extend(sg, ce, s, cc)
                        ce = s
                    except EditError:
                        pass
                if s == ce:
                    cc = crc32_combine(cc, c, e - s)
                    ce = e
                else:                   # a gap that cannot be read (or overlapping entries): keep apart
                    out.append((sg, cs, ce, cc))
                    cs, ce, cc = s, e, c
            out.append((sg, cs, ce, cc))
        return out

    def load_state(self, *, upto: int | None = None, progress: Progress | None = None,
                   cancel: CancelToken | None = None) -> OverlayState:
        """The state at ``upto`` (default: head): the checkpoint, then every step replayed (the piece
        table from the nearest snapshot). Every span and layer file is validated; the first bad step
        raises :class:`LayerDamagedError` (``.seq``, ``.last_good``). ``progress`` counts steps."""
        with self._lock:
            self._check_open()
            target = self.head if upto is None else _uint(upto, "upto")
            if not self.created:
                return OverlayState.initial(self.base_count, self.reader)
            if not self._cp_seq <= target <= self._max_seq:
                raise ValueError(f"cannot load step {target} (steps {self._cp_seq}..{self._max_seq})")
            opened_before = set(self._layers)
            try:
                return self._replay(target, progress, cancel)
            except BaseException:
                for name in set(self._layers) - opened_before:
                    layer = self._layers.pop(name)
                    layer.close()
                raise

    def _replay(self, target: int, progress: Progress | None, cancel: CancelToken | None) -> OverlayState:
        assert self._con is not None
        state = self._checkpoint_state()
        cp = self._cp_seq
        if progress is not None:
            progress.set_total(target - cp)
        try:
            rows = self._con.execute("SELECT seq, ts, data FROM steps WHERE seq > ? AND seq <= ? ORDER BY seq",
                                     (cp, target)).fetchall()
        except sqlite3.Error as exc:
            raise _damaged(cp + 1, f"database: {exc}") from exc
        steps: list[Step] = []
        sizes: dict[int, int | None] = {}
        expect = cp + 1
        for seq, ts, text in rows:
            check(cancel)
            if seq != expect:
                raise _damaged(expect, "step row missing")
            try:
                data = _loads(text)
                ends = {int(k): int(v) for k, v in (data.get("ends") or {}).items()}
                crcs = _crc_entries(data.get("crc"))
            except (ValueError, TypeError, AttributeError) as exc:
                raise _damaged(seq, f"step data: {exc}") from exc
            self._check_ends(ends, seq, sizes=sizes)
            self._verify_crcs(crcs, seq, cancel=cancel)
            try:
                st = Step.from_json(data, self._own_reader.read, seq=seq, ts=ts)
            except (ValueError, TypeError) as exc:
                raise _damaged(seq, f"step data: {exc}") from exc
            except EditError as exc:
                raise _damaged(seq, f"step blob: {exc.params.get('detail') or exc.key}") from exc
            for _rid, _old, new in st.content:
                if new is not None and not _span_ok(new, ends, self.data_size):
                    raise _damaged(seq, f"content span {new} outside the committed bytes")
            if st.wrapper is not None:
                for sp in st.wrapper:
                    if sp is not None and not _span_ok(sp, ends, self.data_size):
                        raise _damaged(seq, f"wrapper span {sp} outside the committed bytes")
            if st.pieces_blob is not None and not _span_ok(st.pieces_blob, ends, self.data_size):
                raise _damaged(seq, "piece-table snapshot outside the committed bytes")
            self._summ[seq] = _summary(st)
            steps.append(st)
            expect += 1
            if progress is not None:
                progress.update(seq - cp)
        if expect != target + 1:
            raise _damaged(expect, "step row missing")
        blob_i = -1
        for i in range(len(steps) - 1, -1, -1):
            if steps[i].pieces_blob is not None:
                blob_i = i
                break
        pieces = state.pieces
        layers = list(state.layers)
        top = layers.pop()
        pending: dict[int, tuple[int, int] | None] = {}
        wrapper, hole, flags = state.wrapper, state.wrapper_hole, state.flags
        next_rid = state.next_rid
        for i, st in enumerate(steps):
            check(cancel)
            try:
                if i == blob_i:
                    pieces = PieceTable.from_bytes(self._read_blob(st.pieces_blob))  # type: ignore[arg-type]
                elif i > blob_i and st.remap is not None:
                    pieces = pieces.apply(st.remap, new_rids=st.new_rids)
                for rid, old, new in st.content:
                    cur = pending[rid] if rid in pending else top.get(rid)
                    if cur != old:
                        raise ValueError(f"record {rid}: the content does not match the step")
                    pending[rid] = new
                if st.push_layer is not None:
                    top = top.with_changes(pending)
                    pending = {}
                    layers.append(top)
                    layers.append(self.layer(st.push_layer))
                    top = DictLayer()
                if st.wrapper is not None:
                    ow, nw, oh, nh = st.wrapper
                    if (wrapper, hole) != (ow, oh):
                        raise ValueError("the wrapper does not match the step")
                    wrapper, hole = nw, nh
                if st.flags is not None:
                    flags = st.flags[1]
                next_rid = max(next_rid, step_next_rid(st))
            except LayerDamagedError:
                raise
            except (ValueError, IndexError, EditError) as exc:
                raise _damaged(st.seq, str(exc)) from exc
        layers.append(top.with_changes(pending))
        try:
            out = state.derive(pieces=pieces, layers=tuple(layers), wrapper=wrapper, wrapper_hole=hole, flags=flags,
                               next_rid=max(next_rid, self._next_rid_hw), version=target)
        except (ValueError, TypeError) as exc:
            raise _damaged(target, str(exc)) from exc
        if target == self.head:
            self._mirror = (target, pieces)
        if progress is not None:
            progress.finish()
        return out

    # -- trimming --------------------------------------------------------------------------------
    def maybe_trim(self, limit: int) -> None:
        """Keep at most ``limit + 64`` undoable steps: while there are more, the oldest 64 are folded
        into the checkpoint (replayed forward from the in-memory checkpoint state; one transaction)."""
        with self._lock:
            if self.read_only or self.closed or self._discarded or not self.created:
                return
            limit = max(0, int(limit))
            while self.head - self._cp_seq > limit + TRIM_BATCH:
                self._fold(self._cp_seq + TRIM_BATCH)

    def _row_crcs(self, lo: int, hi: int) -> list[tuple[int, int, int, int]]:
        """The checksum entries of the step rows ``lo+1 .. hi``."""
        assert self._con is not None
        out: list[tuple[int, int, int, int]] = []
        try:
            rows = self._con.execute("SELECT seq, data FROM steps WHERE seq > ? AND seq <= ? ORDER BY seq",
                                     (lo, hi)).fetchall()
        except sqlite3.Error as exc:
            raise _write_error(exc, self.dir / DB_NAME) from exc
        for seq, text in rows:
            try:
                out.extend(_crc_entries(_loads(text).get("crc")))
            except (ValueError, TypeError, AttributeError) as exc:
                raise _damaged(seq, f"step data: {exc}") from exc
        return out

    def _rows_point_start(self, after: int) -> int | None:
        """Where the point-segment bytes of the step rows after ``after`` begin (rows are committed in seq
        order, so the first row with a point entry has the lowest start); ``None`` when no row has one, 0
        when that cannot be told."""
        assert self._con is not None
        try:
            for (text,) in self._con.execute("SELECT data FROM steps WHERE seq > ? ORDER BY seq", (after,)):
                for sg, s, _e, _c in _crc_entries(_loads(text).get("crc")):
                    if sg == self._point_seg:
                        return s
        except (sqlite3.Error, ValueError, TypeError, AttributeError):
            return 0
        return None

    def _fold(self, new_cp: int) -> None:
        state = self._checkpoint_state()
        for seq in range(self._cp_seq + 1, new_cp + 1):
            state = apply_step(state, self.step(seq), layers=self)
        folded_crcs = self._row_crcs(self._cp_seq, new_cp)
        old_blobs = self._cp_blobs

        def reuse(obj: Any) -> list[int] | None:
            for o, span in old_blobs:
                if o is obj:
                    return span
            return None

        blobs: list[tuple[Any, list[int]]] = []
        pieces = state.pieces
        if pieces == PieceTable.identity(pieces.count):
            pj: dict[str, Any] = {"identity": pieces.count}
        else:
            span = reuse(pieces) or self._blob_writer(pieces.to_bytes())
            blobs.append((pieces, span))
            pj = {"blob": span}
        lj: list[dict[str, Any]] = []
        deps = {self._point_seg}
        for layer in state.layers:
            if isinstance(layer, ArrayLayer):
                lj.append({"array": layer.name})
                deps.update(layer.segments)
            elif not len(layer):
                lj.append({"dict": None})
            else:
                span = reuse(layer) or self._blob_writer(layer.to_bytes())
                blobs.append((layer, span))
                lj.append({"dict": span})
        self.sync(deps)
        ends = {sg: self._published.get(sg, 0) for sg in sorted(deps) if sg in self._published}
        # the checkpoint's checksums: its previous ones, those of the folded steps and of its new blobs
        own_crcs = [tuple(x) for x in self._new_crcs(deps)]
        low = self._rows_point_start(new_cp)
        if low is None:
            low = min([e[1] for e in own_crcs if e[0] == self._point_seg] or [self._published.get(self._point_seg, 0)])
        cp_crcs = self._merge_crcs([*self._cp_crcs, *folded_crcs, *own_crcs], low)  # type: ignore[list-item]
        data = {"v": 1, "pieces": pj, "layers": lj, "wrapper": None if state.wrapper is None else list(state.wrapper),
                "hole": None if state.wrapper_hole is None else list(state.wrapper_hole), "flags": state.flags,
                "next_rid": state.next_rid, "ends": {str(k): v for k, v in ends.items()},
                "crc": [list(x) for x in cp_crcs]}
        files = {name: (0 if owner <= new_cp else owner) for name, owner in self._files.items()}
        new_ends = dict(self._ends)
        for sg, e in ends.items():
            if e > new_ends.get(sg, -1):
                new_ends[sg] = e
        now = time.time()
        text = _dumps(data)

        def write(con: sqlite3.Connection) -> None:
            con.execute("DELETE FROM steps WHERE seq <= ?", (new_cp,))
            con.execute("INSERT OR REPLACE INTO checkpoint(id, seq, data) VALUES (1, ?, ?)", (new_cp, text))
            self._set_meta(con, files=files, ends=new_ends, updated=now)

        self._txn(write, "trim")
        self._crcs_recorded(deps)
        self._cp_seq = new_cp
        self._cp_state = state
        self._cp_blobs = blobs
        self._cp_crcs = cp_crcs
        self._files = files
        self._ends = new_ends
        for s in [s for s in self._summ if s <= new_cp]:
            del self._summ[s]

    # -- journal / discard / close ------------------------------------------------------------------
    def journal(self, data: dict | None) -> None:
        """Store (``data``) or clear (``None``) the save journal (§12.7); durable."""
        with self._lock:
            self._check_writable()
            if data is None and not self.created:
                self.saving = None
                return
            value = None if data is None else _json_value(dict(data), "journal")
            self._ensure_created()
            now = time.time()
            self._txn(lambda con: self._set_meta(con, saving=value, updated=now), "journal")
            self.saving = value

    def mark_discarded(self) -> None:
        """Tombstone the layer (meta ``state = "discarded"``, durable; its index entry removed): it is
        never offered for recovery again, and leftovers are purged later. Allowed on read-only stores
        too (deleting a pending or stale layer after an export)."""
        with self._lock:
            self._check_open()
            if self._discarded:
                return
            if self.created:
                now = time.time()
                self._txn(lambda con: self._set_meta(con, state="discarded", updated=now), "discard")
            self._discarded = True
            if self.created and self.location in ("appdata", "beside"):
                _index_remove(key=self.key, dir_=self.dir)

    def discard(self) -> None:
        """Tombstone, then delete the layer's files (best effort) and close the store. Files that alive
        states (pinned views, leases, the document's current state) may still read are deleted when the
        last of them is gone, so those states keep working."""
        with self._lock:
            if self.closed:
                return
            self.mark_discarded()
            _crash_point("discard.marked")
            self._shutdown(delete=True)

    def close(self, *, close_reader: bool = False) -> None:
        """Release the lock and close the database (files kept); a layer at head 0 without a save
        journal is deleted. ``close_reader=True`` also closes the public reader and the cached array
        layers (only for owners that handed them to nobody)."""
        with self._lock:
            if self.closed:
                return
            delete = (self.created and not self.read_only and not self._discarded and self.head == 0
                      and not self.saving)
            if self.created and not self.read_only and not self._discarded and not delete:
                for seg in sorted(self._alloc_segs):           # previews never applied
                    if segment_name(seg) not in self._files:
                        w = self._writers.pop(seg, None)
                        if w is not None:
                            w.f.close()
                        self.reader.forget(seg)
                        self._own_reader.forget(seg)
                        _remove(self.segment_path(seg))
                for name in sorted(self._alloc_layers):
                    if name not in self._files:
                        layer = self._layers.pop(name, None)
                        if layer is not None:
                            layer.close()
                        _remove(self.dir / name)
                if self.location in ("appdata", "beside"):
                    s = self._summ.get(self.head)
                    extra = {} if s is None else {"label_key": s.label_key, "label_params": s.label_params}
                    _index_update(self.key, steps=self.head, updated=time.time(), **extra)
            if delete and self.location in ("appdata", "beside"):
                _index_remove(key=self.key, dir_=self.dir)
            layers = list(self._layers.values())
            self._shutdown(delete=delete)
            if close_reader:
                self.reader.close()
                for layer in layers:
                    layer.close()

    def _shutdown(self, *, delete: bool) -> None:
        for w in self._writers.values():
            try:
                w.f.close()
            except OSError:  # pragma: no cover
                pass
        self._writers.clear()
        if self._con is not None:
            try:
                self._con.close()
            except sqlite3.Error:  # pragma: no cover
                pass
            self._con = None
        self._own_reader.close()
        self._layers.clear()
        self._cp_state = None        # the store's own copy reads nothing any more (not a witness)
        self._cp_blobs = []
        if delete and self.created:
            _crash_point("close.delete")
            _purge_dir(self.dir)     # files alive states (pinned views, leases) still read: deleted later
        self.closed = True
        for lk in self._extra_locks:
            lk.release()
        self._extra_locks = []
        if self._own_lock:
            self.lock.release()


# ----------------------------------------------------------------------------------
# checksums of the layer's bytes (crc32 per step of the bytes it appended; zlib's crc32_combine)
# ----------------------------------------------------------------------------------
_CRC_READ = 1 << 20
_CRC_PARALLEL_MIN = 64 << 20               # a checked range this big is read by several threads (_crc_range) ...
_CRC_THREADS = 4                           # ... at most this many
_CRC_ZERO_OPS: list[list[int]] = []        # [k]: the GF(2) operator that appends 2**k zero bytes to a crc32
_CRC_OPS_LOCK = threading.Lock()


def _gf2_times(mat: Sequence[int], vec: int) -> int:
    out = 0
    i = 0
    while vec:
        if vec & 1:
            out ^= mat[i]
        vec >>= 1
        i += 1
    return out


def _gf2_square(mat: Sequence[int]) -> list[int]:
    return [_gf2_times(mat, row) for row in mat]


def _crc_zero_op(k: int) -> list[int]:
    with _CRC_OPS_LOCK:
        while len(_CRC_ZERO_OPS) <= k:
            if not _CRC_ZERO_OPS:
                op = [0xEDB88320] + [1 << n for n in range(31)]     # one zero bit
                for _ in range(3):                                   # -> one zero byte
                    op = _gf2_square(op)
                _CRC_ZERO_OPS.append(op)
            else:
                _CRC_ZERO_OPS.append(_gf2_square(_CRC_ZERO_OPS[-1]))
        return _CRC_ZERO_OPS[k]


def crc32_combine(crc1: int, crc2: int, len2: int) -> int:
    """crc32 of ``A + B`` from ``crc32(A)``, ``crc32(B)`` and ``len(B)`` (zlib's ``crc32_combine``)."""
    k = 0
    while len2 > 0:
        if len2 & 1:
            crc1 = _gf2_times(_crc_zero_op(k), crc1)
        len2 >>= 1
        k += 1
    return crc1 ^ crc2


def _crc_entries(value: Any) -> list[tuple[int, int, int, int]]:
    """The ``[seg, start, end, crc32]`` entries of a step / checkpoint row (``ValueError`` when invalid)."""
    out = []
    for item in value or ():
        if not isinstance(item, (list, tuple)) or len(item) != 4:
            raise ValueError("invalid checksum entry")
        sg, s, e, c = (_uint(x, "checksum entry") for x in item)
        if s > e or e >= SEG_SIZE_LIMIT or c > 0xFFFFFFFF or sg > MAX_SEGMENT:
            raise ValueError("invalid checksum entry")
        out.append((sg, s, e, c))
    return out


def _span_ok(span: tuple[int, int], ends: Mapping[int, int], data_size: int) -> bool:
    """A span lies inside the data file or inside the committed bytes of one segment."""
    s, e = span
    if s < EDIT_BASE:
        return e <= data_size if data_size else e < EDIT_BASE
    try:
        seg, off = split_edit_offset(s)
    except ValueError:
        return False
    end_off = e - (s - off)
    return end_off <= ends.get(seg, -1)
