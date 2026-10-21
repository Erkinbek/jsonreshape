"""RecordSet storage, streaming filter and external sort (docs/ARCHITECTURE.md §6).

Requirements: AGENT_PROMPT §3 "Yozuvlar to'plami (RecordSet)", §6.3 (filter), §6.4 (sort),
§12 (10 million records sorted by one field in < 2 minutes with < 1 GB RAM) and §14 tests
17 (stable, typed, natural sort; external sort == in-memory sort) and 18 (filter
expressions and the visual builder give identical results).

RecordSet
=========
An ordered list of 0-based record indices (``uint64``):

* :meth:`RecordSet.base` -- the virtual identity set of a document (all records in file
  order); ``len()`` follows ``doc.record_count`` live while the index is being built and
  ``complete`` is ``doc.index_complete``. Nothing is stored.
* every other set is written by a :class:`RecordSetBuilder` (``append`` / ``extend`` /
  ``finish`` / ``abort``). At most ~1 MiB of indices is buffered in memory; older ones go
  to a temporary file ``recordsets_dir()/rs-<pid>-<hex>.u64`` (raw u64 little-endian).
  The set (``builder.recordset``) is readable *while it grows*: ``len()``, ``[]`` and
  ``iter_chunks()`` work from other threads (e.g. the GUI shows a filter result as it
  fills) and never wait for the builder's file writes (the lock is held for in-memory
  work only; a buffer being written is read from memory, the file through the readers'
  own handle -- see ``_Storage``). :meth:`RecordSetBuilder.finish` turns the same object into a complete set: the
  file is memory-mapped read-only (never read into RAM), or -- for small sets of at most
  :data:`MEMORY_MAX_BYTES` (8192 indices) -- kept in an ``array('Q')`` and the file is
  removed (no file is created at all when nothing was flushed).
* :meth:`RecordSet.close` / :meth:`RecordSet.delete` release the mapping and remove the
  file (Windows: the memoryviews and the mapping are released *before* the file is
  deleted; deletion is retried briefly when another program, e.g. a virus scanner, holds
  the file). A set that is garbage collected or still open at interpreter exit is closed
  the same way (``weakref.finalize``). Files left behind by a crashed process are removed
  by :func:`purge_stale_files` (run once per process when the first builder is created).
* A closed (or aborted) set behaves as an empty set (``len() == 0``; ``closed`` is
  ``True``) so that a view still holding it never crashes; ``close`` must not race with
  reads of a complete set from other threads (like ``index.IndexReader``).
* ``is_ascending()`` (strictly increasing indices) is tracked while building, so readers
  can use contiguous reads.

filter_records
==============
Iterates ``doc.iter_blocks(source)`` and keeps, in source order, the records matched by a
:class:`~jsonreshape.query.Filter`. Two evaluation strategies give identical results
(tested):

* ``"extract"`` (``strategy="auto"`` picks it when ``flt.extractable``, no required
  pattern is ``$`` and the Rust core is active -- the pure-Python reference extractor is
  slower than parsing): one ``core.PathExtractor`` per filter runs over each block
  (``member_spans`` = ``doc.member_records``); only the extracted value spans are decoded
  (scalars directly, containers with :func:`jsonreshape.codec.loads`) and
  ``flt.matches_values`` decides.
  Filters that need no record data (``$index < 10``, ``$key startswith "a"``) parse
  nothing at all.
* ``"parse"``: ``codec.loads`` of every record (member records: ``split_member`` -> key +
  value) and ``flt.matches(value, key=, index=)``. JMESPath filters always use it.

Huge records (``Block.huge``, larger than ``Settings.large_node_bytes``) are evaluated
with :meth:`Filter.matches_stream <jsonreshape.query.Filter.matches_stream>`: ``len()``,
``exists``, ``is empty`` and comparisons of paths with constants are decided on the ijson
event stream without keeping values; the values other parts of the expression need are
captured within the memory budget :func:`jsonreshape.stream.capture_budget`
(``min(large_node_bytes, memory_limit_bytes // 4)``, at least 1 MiB). A
record whose values exceed the budget is skipped and counted in
``FilterReport.too_large`` (warning ``recordset.warn.too_large`` naming the first such
record). JMESPath filters and filters that use the whole record ``$`` as a value cannot
be evaluated on huge records without loading them, so such records are skipped and
counted in ``FilterReport.skipped_huge`` (warning ``recordset.warn.skipped_huge``). A
record that cannot be decoded (e.g. nested deeper than the Python JSON decoder allows,
or bytes changed on disk after indexing) is evaluated by streaming it with ijson instead;
only when that fails too it is skipped and counted in ``FilterReport.unparseable``
(warning). A JMESPath expression that fails on one record (``contains(tags, 'x')`` with
``tags: null``) makes that record not match; such records are counted in
``FilterReport.eval_errors`` (warning ``recordset.warn.eval_errors`` with the first
record number and the localized reason) -- the filter never aborts because of one
record. But an expression that fails on *every* record it is evaluated on -- the first
:data:`ALL_FAILED_LIMIT` (1000) of them, or all of them when there are fewer -- is
wrong rather than meeting odd records (``abs(tags)`` on an array field), and an empty
result would hide that: the filter stops with the localized
:class:`~jsonreshape.errors.UserInputError` ``recordset.error.eval_failed`` naming the
first failing record and the reason (the evaluator's own text in the hint; the CLI exits
with 1). Warnings with a count use the ``<key>.one`` text when the count is 1 and show
counts with grouped digits (:meth:`FilterReport.warning_texts`).
``$index`` is the record number in the file, ``$key`` the member key of keyed
records. ``on_batch(recordset)`` is called with the growing result at most every
:data:`BATCH_INTERVAL` seconds when it has grown. When the scan has finished,
``doc.ensure_unchanged()`` makes sure the file was not changed by another program
meanwhile (:class:`~jsonreshape.errors.FileChangedError`).

sort_records
============
Field values are extracted *without parsing whole records*: one ``core.PathExtractor``
with every key's ``core_path()`` (the first match per path in document order is used,
like ``SortKey.extract``); the raw value bytes go through ``sortkeys.token_encoder(spec)``
(a missing value: ``encoder(None)``); ``$key`` / ``$index`` use
``sortkeys.value_encoder(spec)`` on the member key (``paths.MISSING`` for other formats) /
the record number. Huge records use ``stream_values`` + ``value_encoder``. The composite
key of a record is the concatenation of its field keys plus the 8-byte big-endian
sequence number (position in the source), so the order is total and **stable**.

Keys are sorted in memory while their estimated size (``len(key) + ENTRY_OVERHEAD`` bytes
per entry: the ``bytes`` object header plus the list slot) stays below the sort's memory
limit: ``memory_limit`` (default ``Settings.sort_memory_bytes`` of the document), but at
most half of the overall budget ``Settings.memory_limit_bytes`` ("xotira chegarasi");
beyond that, sorted runs are written to ``appdirs.work_dir(settings.cache_dir)/sort-<pid>-<hex>/``
(``<cache folder>/tmp`` when a cache folder is configured, else the app's ``tmp``;
length-prefixed keys, buffered I/O) and k-way merged with ``heapq.merge`` (at most
:data:`MAX_FANIN` runs per pass; more runs are first merged into bigger runs). The number
of runs is kept to about :data:`TARGET_RUNS`: when the keys seen so far project more runs
(a tiny limit such as ``--memory-limit 512``), the run size grows -- up to half of
``memory_limit_bytes`` -- instead of writing tens of thousands of files. The last, still
in-memory part takes part in the final merge without being written; the read buffers of
the runs together stay below ``limit / 2`` (at least 64 KiB each). The temporary
directory is always removed (success, error, cancellation). The result is the RecordSet
of ``source[seq]`` in sorted order. ``SortReport.missing`` counts, per sort key, the
records without a value; a key missing in every record, or in more than 99 % of them
(e.g. a misspelled field), gives a warning (``recordset.warn.sort_key_absent`` /
``recordset.warn.sort_key_rare``). When the scan has finished, ``doc.ensure_unchanged()``
makes sure the file was not changed meanwhile.

Cancellation is checked between blocks, for every slice and merge batch of the in-memory
sort (:func:`_sorted_pieces`), every :data:`RUN_WRITE_ITEMS` keys written to a run and
every merged output batch: a cancelled sort of 10 million records stops within a few ms.
The key lists are emptied as soon as the sort ends early (an exception's traceback would
keep them alive) -- millions of keys by a short daemon thread, :data:`RELEASE_SLICE` at a
time (``_release``: the task ends at once and no single call holds the GIL for the
~0.2 s that freeing 4 million keys takes) -- and every sort ends with
``sortkeys.clear_caches()``: the encoders' memoised
keys were allocated among the sort's keys and each pinned a 1 MiB pymalloc arena of freed
keys, so the process kept ~120 MB after a cancelled 10-million-record sort.

Progress is reported in bytes: record bytes read, then (sort) the bytes of the keys
merged; when the caller's ``Progress`` has no total, the total is set (the data bytes).
A sort never moves its bar back: from the first block on, the total includes the key
bytes projected onto the whole input (the keys collected so far times expected data
bytes / data bytes read) and then stays fixed; the reading phase fills its projected share
of the bar and the merge phase -- reported with its own message,
``recordset.progress.sort_merge`` -- the rest, so ``done`` never exceeds the total and
the remaining time stays above zero until the last keys are merged (a caller's own total
is filled the same way, in proportion).

Decisions and deviations (documented, see also the report of the recordset role)
-------------------------------------------------------------------------------
* Complete sets of at most 8192 indices live in memory instead of a file.
* ``strategy="auto"`` uses ``"parse"`` when the pure-Python core is active (its reference
  ``PathExtractor`` is slower than ``codec.loads``); both strategies give the same result.
* A closed/aborted set reads as empty instead of raising.
* ``source`` may be ``None`` (all records; follows a growing index until it is complete,
  like ``iter_blocks``), ``RecordSet.base(doc)`` (same as ``None``), any other RecordSet,
  a ``range``, a list/tuple/``array`` or any iterable of record indices (sort copies a
  plain iterable into a temporary RecordSet first, because it needs ``source[seq]``). A
  RecordSet that is still growing is read as far as it has grown.
* Sorting by the whole record ``$`` on a *huge* object/array record would need the whole
  record (and a key as large as the record) in memory. Its field key is therefore built
  from the first :data:`HUGE_KEY_PREFIX` bytes of the record's compact JSON text
  (``core.Reformatter``) -- exact whenever records differ within that prefix and the
  record text has no escapes that ``codec.dumps`` would rewrite and no duplicate keys.
  ``SortReport.approximate`` counts the records whose compact text was longer than the
  prefix (warning ``recordset.warn.approximate``). Other fields of huge records come from
  ``stream_values(first_only=True)`` (only the first value of each field is kept), with
  the documented ijson deviations of :mod:`jsonreshape.stream`; a field value larger than
  the capture budget :func:`jsonreshape.stream.capture_budget` (a huge array or
  string) is keyed by its first :data:`HUGE_KEY_PREFIX` bytes / characters
  (:func:`jsonreshape.stream.stream_text_prefix`) and counted in
  ``SortReport.approximate`` as well.
* A record whose extraction/decoding fails during a sort is keyed by streaming it with
  ijson; when that fails too the error is raised (a sort cannot drop records).
* Extra keyword arguments: ``strategy``, ``block_bytes`` and ``batch_interval``
  (filter), ``block_bytes`` (sort); ``filter_records`` also accepts expression text and
  ``sort_records`` key texts such as ``"-id"``.

Edited documents (docs/ARCHITECTURE.md §12.0, §12.3)
-----------------------------------------------------
Record numbers are the *positions* of the edited document. :func:`filter_records` and
:func:`sort_records` begin with ``doc = doc.pinned()`` (the whole run reads one overlay
state), refuse a ``source`` whose ``gen`` belongs to another state
(``EditError("edits.error.stale_scope")``; sources without ``gen`` pass) and tag their
result with the pinned state's ``gen`` (``RecordSet.gen``; builders take ``gen=``).
``RecordSet.base(doc)`` binds the given document (a pinned view: the frozen count of its
state) and is recognized as "all records" for any view of the same live document
(:meth:`RecordSet._is_base_of` compares origins). Huge records are read through
``doc.open_data()``; progress totals of an edited document come from
``doc.effective_bytes()``.
"""

from __future__ import annotations

import bisect
import heapq
import itertools
import logging
import mmap
import operator
import os
import re
import secrets
import shutil
import struct
import sys
import threading
import time
import weakref
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence, Union

from . import codec, core
from .appdirs import recordsets_dir, tmp_dir, work_dir
from .errors import Cancelled, EditError, JsonReshapeError, UserInputError
from .fileio import long_path, open_shared, os_error, read_range
from .i18n import has, space_grouped, t
from .paths import MISSING, PathPattern
from .query import Filter, FilterEvalError, compile_filter
from .reader import Block, split_member
from .settings import get_settings
from .sortkeys import SortKey, clear_caches, describe_keys, token_encoder, value_encoder
from .stream import CaptureLimitExceeded, capture_budget, stream_text_prefix, stream_values
from .tasks import CancelToken, Progress, check, format_bytes

log = logging.getLogger(__name__)

StrPath = Union[str, "os.PathLike[str]"]

__all__ = [
    "ALL_FAILED_LIMIT",
    "BATCH_INTERVAL",
    "BLOCK_BYTES",
    "ENTRY_OVERHEAD",
    "FLUSH_BYTES",
    "FilterReport",
    "HUGE_KEY_PREFIX",
    "MAX_FANIN",
    "MEMORY_MAX_BYTES",
    "RecordSet",
    "RecordSetBuilder",
    "SortReport",
    "TARGET_RUNS",
    "filter_records",
    "purge_stale_files",
    "sort_records",
]

KiB = 1 << 10
MiB = 1 << 20

BLOCK_BYTES = 8 * MiB          # iter_blocks() block size used by the runners
FLUSH_BYTES = 1 * MiB          # RecordSetBuilder: indices buffered in memory before a file write
MEMORY_MAX_BYTES = 64 * KiB    # complete sets up to this size stay in RAM (no file)
CHUNK_ITEMS = 65536            # default iter_chunks() size; batch size of the runners
BATCH_INTERVAL = 0.1           # filter_records: minimum seconds between on_batch() calls
ENTRY_OVERHEAD = 41            # sort: bytes per in-memory key besides its length (bytes header 33 + list slot 8)
MAX_FANIN = 64                 # sort: runs merged at once (more runs -> intermediate merge passes)
SORT_SLICE = 4096              # sort: keys sorted by one list.sort() call (bounds the GIL hold, see _sorted_pieces)
RUN_WRITE_ITEMS = 8192         # sort: keys joined by one bytes.join() when a run is written
SORT_MERGE_FANIN = 8           # sort: sorted slices merged at once (batches <= fan-in * SORT_SLICE keys)
TARGET_RUNS = 256              # sort: the run size grows when more runs than this are projected
RARE_KEY_PERCENT = 99          # sort: warn when a key is missing in more than this % of the records
RUN_READ_BYTES = 1 * MiB       # sort: read buffer per run (shrunk for many runs)
RUN_WRITE_BYTES = 1 * MiB      # sort: write buffer of a run file
HUGE_KEY_PREFIX = 64 * KiB     # sort: compact-text prefix used for '$' keys of huge containers
HUGE_READ_BYTES = 1 * MiB      # sort: read size when streaming a huge record through the Reformatter
ALL_FAILED_LIMIT = 1000        # filter: stop when the expression failed on this many records and on no other
RELEASE_SLICE = 1 << 16        # sort: keys freed per step when an ended sort's lists are released

_BIG_ENDIAN = sys.byteorder == "big"
_WINDOWS = sys.platform == "win32"
_SEQ_PACK = struct.Struct(">Q").pack           # stable-sort suffix: position in the source
_LEN = struct.Struct("<I")                     # run files: u32 LE length prefix per key
_TAIL = operator.itemgetter(slice(-8, None))   # composite key -> its 8-byte sequence suffix
_INV = bytes(range(255, -1, -1))              # descending payloads: b -> 0xFF - b
_ROOT = PathPattern(())
_RS_NAME_RE = re.compile(r"rs-(\d+)-[0-9a-f]+\.u64\Z")
_SORT_DIR_RE = re.compile(r"sort-(\d+)-[0-9a-f]+\Z")
_DECODE_ERRORS = (codec.JsonDecodeError, ValueError, RecursionError, UnicodeDecodeError)


# ----------------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------------
def _u64(data: bytes | bytearray | memoryview) -> array:
    """u64 LE bytes -> ``array('Q')``."""
    a = array("Q")
    a.frombytes(data)
    if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only
        a.byteswap()
    return a


def _remove_file(p: Path, attempts: int = 5) -> bool:
    """Remove ``p``; on Windows retry briefly while another program holds it. True when
    the file is gone."""
    delay = 0.01
    for attempt in range(attempts):
        try:
            os.remove(long_path(p))
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if not _WINDOWS or attempt == attempts - 1:
                break
            time.sleep(delay)
            delay *= 2
        except OSError:
            break
    log.warning("could not remove temporary file %s (removed later)", p)
    return False


def _remove_tree(p: Path) -> bool:
    """Remove the directory ``p`` with retries (Windows). True when it is gone."""
    delay = 0.01
    for attempt in range(5):
        try:
            shutil.rmtree(long_path(p))
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if not _WINDOWS or attempt == 4:
                break
            time.sleep(delay)
            delay *= 2
    log.warning("could not remove temporary directory %s (removed later)", p)
    return False


def _pid_alive(pid: int) -> bool:
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a dependency
        return True
    try:
        return bool(psutil.pid_exists(pid))
    except Exception:  # pragma: no cover - be conservative
        return True


def _sort_dirs() -> list[Path]:
    """Folders that may hold sort run directories: the app's ``tmp`` and the work folder
    of the configured cache folder (``appdirs.work_dir``)."""
    out = [tmp_dir()]
    try:
        cache_dir = get_settings().cache_dir
        if cache_dir:
            w = work_dir(cache_dir)
            if w not in out:
                out.append(w)
    except Exception:  # pragma: no cover - settings / cache folder unusable
        log.debug("cannot use the cache folder's work directory", exc_info=True)
    return out


def purge_stale_files() -> int:
    """Remove record-set files (``recordsets_dir()/rs-<pid>-*.u64``) and sort run
    directories (``sort-<pid>-*`` in ``tmp_dir()`` and in the cache folder's work
    directory) of processes that no longer run (left by a crash or by a file Windows
    refused to delete). Never raises; returns the number of entries removed."""
    removed = 0
    me = os.getpid()
    try:
        places = [(recordsets_dir(), _RS_NAME_RE, False)] + [(d, _SORT_DIR_RE, True) for d in _sort_dirs()]
    except OSError:  # pragma: no cover - app dir not creatable
        return 0
    for base, rx, is_dir in places:
        try:
            names = os.listdir(long_path(base))
        except OSError:
            continue
        for name in names:
            m = rx.match(name)
            if m is None:
                continue
            pid = int(m.group(1))
            if pid == me or _pid_alive(pid):
                continue
            p = base / name
            try:
                if is_dir:
                    shutil.rmtree(long_path(p))
                else:
                    os.remove(long_path(p))
                removed += 1
                log.info("removed stale temporary %s", p)
            except OSError:
                log.debug("cannot remove stale %s", p, exc_info=True)
    return removed


_purged = False
_purge_lock = threading.Lock()


def _purge_once() -> None:
    global _purged
    if _purged:
        return
    with _purge_lock:
        if _purged:
            return
        _purged = True
    try:
        purge_stale_files()
    except Exception:  # pragma: no cover - best effort
        log.debug("purge of stale record-set files failed", exc_info=True)


def _index_array(items: Any) -> array:
    """``items`` (array, range, list, tuple or iterable of ints) as ``array('Q')``;
    ``ValueError`` for negative or too large values, ``TypeError`` for non-integers."""
    if isinstance(items, array) and items.typecode == "Q":
        return items
    if isinstance(items, (str, bytes, bytearray, memoryview)):
        raise TypeError(f"record indices must be integers, not {type(items).__name__}")
    try:
        return array("Q", items)
    except OverflowError:
        raise ValueError("record indices must be integers in [0, 2**64)") from None


# ----------------------------------------------------------------------------------
# storage
# ----------------------------------------------------------------------------------
class _BaseStore:
    """Virtual identity storage of :meth:`RecordSet.base`."""

    path = None
    ascending = True

    def __init__(self, doc: Any) -> None:
        self.doc: Any = doc

    @property
    def state(self) -> str:
        return "closed" if self.doc is None else "complete"

    @property
    def complete(self) -> bool:
        doc = self.doc
        return doc is not None and bool(doc.index_complete)

    def count(self) -> int:
        doc = self.doc
        return int(doc.record_count) if doc is not None else 0

    def get(self, i: int) -> int:
        return i

    def get_many(self, i: int, n: int) -> array:
        stop = min(self.count(), i + n)
        return array("Q", range(i, stop)) if stop > i else array("Q")

    def view(self) -> Any:
        return None

    def close(self) -> None:
        self.doc = None


class _Storage:
    """Record indices: *open* (buffer + optional temp file, readable while growing) ->
    *complete* (``array('Q')`` or a read-only mmap) -> *closed*.

    Locks (never held across file I/O by readers' paths, never nested):

    * ``_lock`` guards the in-memory state (buffer, chunks being written, counts, state)
      and is held only for in-memory work -- so a reader (``get`` / ``get_many``, e.g. the
      GUI thread showing a growing filter result) never waits for a file write;
    * ``_wlock`` serializes the writer's file work (creating the file, writing a chunk,
      finishing): a full buffer is moved to ``_pending`` under ``_lock`` (readers serve
      those indices from memory meanwhile) and written under ``_wlock`` only;
    * ``_rlock`` guards the readers' own read handle (indices already in the file are read
      through it, so readers and the writer never share a file position).

    Reads of a complete storage go straight to ``_view`` (an ``array('Q')`` or a
    ``memoryview`` cast to ``"Q"``). One thread appends (the builder's); ``close`` may come
    from any thread.
    """

    def __init__(self, *, directory: StrPath | None = None, flush_bytes: int | None = None,
                 memory_max: int | None = None) -> None:
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self._rlock = threading.Lock()
        self._dir = Path(directory) if directory is not None else None
        self._flush_items = max(1, int(FLUSH_BYTES if flush_bytes is None else flush_bytes) // 8)
        self._memory_max = max(0, int(MEMORY_MAX_BYTES if memory_max is None else memory_max))
        self._buf = array("Q")        # indices not yet handed to the writer
        self._pending: list[list[Any]] = []   # [first index, array, written] being written, in order
        self._pending_n = 0           # indices in _pending
        self._flushed = 0             # indices in the file (from index 0 on)
        self._count = 0
        self._f: Any = None           # raw r+b handle of the writer, created by the first write
        self._rf: Any = None          # the readers' raw read handle (opened on first use)
        self.path: Path | None = None
        self._view: Any = None        # complete: array('Q') or memoryview('Q')
        self._mv: memoryview | None = None
        self._mm: mmap.mmap | None = None
        self.state = "open"           # open | complete | closed
        self._finishing = False
        self.ascending = True
        self._last = -1

    @property
    def complete(self) -> bool:
        return self.state == "complete"

    # -- writing ---------------------------------------------------------------------
    def _check_open(self) -> None:
        if self.state != "open" or self._finishing:
            raise ValueError(f"record set builder is {'closed' if self.state == 'closed' else 'finished'}")

    def append(self, i: int) -> None:
        with self._lock:
            self._check_open()
            try:
                self._buf.append(i)
            except OverflowError:
                raise ValueError(f"record index out of range: {i!r}") from None
            if self.ascending and i <= self._last:
                self.ascending = False
            self._last = i
            self._count += 1
            entry = self._take_locked() if len(self._buf) >= self._flush_items else None
        if entry is not None:
            self._write(entry)

    def extend(self, arr: array) -> None:
        if not arr:
            return
        with self._lock:
            self._check_open()
            if self.ascending:
                if arr[0] <= self._last or (
                        len(arr) > 1 and not all(map(operator.lt, arr, itertools.islice(arr, 1, None)))):
                    self.ascending = False
            self._last = arr[-1]
            self._buf.extend(arr)
            self._count += len(arr)
            entry = self._take_locked() if len(self._buf) >= self._flush_items else None
        if entry is not None:
            self._write(entry)

    def _take_locked(self) -> list[Any]:
        """Move the buffer to ``_pending`` (``_lock`` held); the caller writes it with
        :meth:`_write` after releasing ``_lock``."""
        buf = self._buf
        self._buf = array("Q")
        entry = [self._flushed + self._pending_n, buf, False]
        self._pending.append(entry)
        self._pending_n += len(buf)
        return entry

    def _create_file(self) -> None:
        """Create the temporary file (``_wlock`` held)."""
        base = self._dir if self._dir is not None else recordsets_dir()
        try:
            os.makedirs(long_path(base), exist_ok=True)
        except OSError as exc:
            raise os_error(exc, base) from exc
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        for _ in range(100):
            p = base / f"rs-{os.getpid()}-{secrets.token_hex(6)}.u64"
            try:
                fd = os.open(long_path(p), flags, 0o666)
                break
            except FileExistsError:
                continue
            except OSError as exc:
                raise os_error(exc, p) from exc
        else:  # pragma: no cover
            raise JsonReshapeError("recordset.error.write", path=str(base), detail="no temporary name")
        try:
            self._f = os.fdopen(fd, "r+b", buffering=0)
        except BaseException:
            os.close(fd)
            _remove_file(p)
            raise
        self.path = p

    def _write(self, entry: list[Any]) -> None:
        """Write a chunk taken by :meth:`_take_locked` (``_wlock`` only: readers go on);
        then the file holds it and ``_flushed`` moves on."""
        with self._wlock:
            if self.state == "closed":
                return
            if self._f is None:
                self._create_file()
            buf = entry[1]
            data = buf.tobytes() if not _BIG_ENDIAN else self._swapped(buf)
            try:
                self._f.seek(8 * entry[0])
                with memoryview(data) as view:
                    done = 0
                    while done < len(view):
                        done += self._f.write(view[done:])
            except OSError as exc:
                raise os_error(exc, self.path or Path(".")) from exc
        with self._lock:
            entry[2] = True
            pending = self._pending
            while pending and pending[0][2]:
                done_entry = pending.pop(0)
                n = len(done_entry[1])
                self._flushed += n
                self._pending_n -= n

    @staticmethod
    def _swapped(a: array) -> bytes:  # pragma: no cover - big-endian hosts only
        b = array("Q", a)
        b.byteswap()
        return b.tobytes()

    def _read_file(self, i: int, n: int) -> array | None:
        """Indices ``i .. i+n-1`` from the file through the readers' handle (``_rlock``
        only); ``None`` when the storage is no longer open (the caller looks again)."""
        with self._rlock:
            if self.state != "open":
                return None
            f = self._rf
            if f is None:
                p = self.path
                if p is None:   # pragma: no cover - flushed indices imply a file
                    return None
                try:
                    f = self._rf = open(long_path(p), "rb", buffering=0)  # noqa: SIM115 - closed in close()/finish()
                except OSError as exc:
                    raise os_error(exc, p) from exc
            try:
                return _u64(read_range(f, 8 * i, 8 * (i + n)))
            except OSError as exc:
                raise os_error(exc, self.path or Path(".")) from exc

    def _close_reader(self) -> None:
        with self._rlock:
            f, self._rf = self._rf, None
        if f is not None:
            try:
                f.close()
            except OSError:  # pragma: no cover
                pass

    def finish(self) -> None:
        """Make the storage complete: small sets -> ``array('Q')`` (file removed), larger
        ones -> read-only memory map of the file. The last chunk is written and the file
        mapped without ``_lock`` (readers go on meanwhile)."""
        with self._lock:
            self._check_open()
            self._finishing = True
            small = 8 * self._count <= self._memory_max
            entry = self._take_locked() if not small and self._buf else None
        try:
            if entry is not None:
                self._write(entry)
            if small:
                self._finish_small()
            else:
                self._finish_mapped()
        except BaseException:
            with self._lock:
                self._finishing = False
            raise
        self._close_reader()

    def _finish_small(self) -> None:
        with self._wlock:
            f, p = self._f, self.path
            arr = array("Q")
            if f is not None and self._flushed:
                try:
                    arr = _u64(read_range(f, 0, 8 * self._flushed))
                except OSError as exc:
                    raise os_error(exc, p or Path(".")) from exc
            with self._lock:
                if self.state == "closed":
                    return
                arr.extend(self._buf)
                self._buf = array("Q")
                self._view = arr
                self._f = None
                self.path = None
                self.state = "complete"
        if f is not None:
            try:
                f.close()
            except OSError:  # pragma: no cover
                pass
            self._close_reader()
            if p is not None:
                _remove_file(p)

    def _finish_mapped(self) -> None:
        with self._wlock:
            f = self._f
            if self.state == "closed" or f is None:
                return
            if _BIG_ENDIAN:  # pragma: no cover - mmap views assume little-endian u64
                view: Any = _u64(read_range(f, 0, 8 * self._flushed))
                mm = mv = None
            else:
                try:
                    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                except OSError as exc:
                    raise os_error(exc, self.path or Path(".")) from exc
                mv = memoryview(mm)
                view = mv.cast("Q")
            with self._lock:
                self._mm = mm
                self._mv = mv
                self._view = view  # published last: lock-free readers use _mv after seeing _view
                self._buf = array("Q")
                self.state = "complete"
            self._f = None
        try:
            f.close()  # the mapping keeps its own handle
        except OSError:  # pragma: no cover
            pass

    # -- reading ---------------------------------------------------------------------
    def count(self) -> int:
        view = self._view
        if view is not None:
            return len(view)
        return self._count

    def view(self) -> Any:
        """The complete index array (``array`` or ``memoryview``), else ``None``."""
        return self._view

    def get(self, i: int) -> int:
        while True:
            view = self._view
            if view is not None:
                return view[i]
            with self._lock:
                view = self._view
                if view is not None:
                    return view[i]
                if self.state == "closed" or i >= self._count:
                    raise IndexError(f"record set index {i} out of range")
                fl = self._flushed
                if i >= fl:
                    bstart = fl + self._pending_n
                    if i >= bstart:
                        return self._buf[i - bstart]
                    for start, chunk, _written in self._pending:
                        if i < start + len(chunk):
                            return chunk[i - start]
            out = self._read_file(i, 1)      # flushed: from the file, without _lock
            if out is not None:
                return out[0]
            # finished or closed meanwhile: look again

    def get_many(self, i: int, n: int) -> array:
        while True:
            view = self._view
            if view is not None:
                break
            with self._lock:
                view = self._view
                if view is not None:
                    break
                stop = min(self._count, i + n)
                if self.state == "closed" or i >= stop:
                    return array("Q")
                fl = self._flushed
                tail = array("Q")             # the part not in the file yet (copied under _lock)
                if stop > fl:
                    pos = max(i, fl)
                    for start, chunk, _written in self._pending:
                        a = max(pos, start)
                        b = min(stop, start + len(chunk))
                        if a < b:
                            tail.extend(chunk[a - start:b - start])
                    bstart = fl + self._pending_n
                    if stop > bstart:
                        tail.extend(self._buf[max(pos, bstart) - bstart:stop - bstart])
            if i >= fl:
                return tail
            out = self._read_file(i, min(stop, fl) - i)   # without _lock
            if out is not None:
                out.extend(tail)
                return out
            # finished or closed meanwhile: look again
        if isinstance(view, array):
            return view[i:i + n]
        mv = self._mv  # the mapping's byte view (array.frombytes needs format "B")
        out = array("Q")
        if mv is not None:
            out.frombytes(mv[8 * i:8 * (i + n)])
        return out

    # -- closing ---------------------------------------------------------------------
    def close(self) -> None:
        """Release the mapping/handles and remove the file (idempotent; never raises).
        Waits for a chunk write in progress (``_wlock``) and a reader's file read
        (``_rlock``), never for ``_lock`` holders doing I/O (there are none)."""
        with self._lock:
            if self.state == "closed":
                return
            self.state = "closed"
            view, mv, mm = self._view, self._mv, self._mm
            self._view = self._mv = self._mm = None
            self._buf = array("Q")
            self._pending = []
            self._count = self._flushed = self._pending_n = 0
        with self._wlock:            # a write in progress ends first; no new file after this
            f, p = self._f, self.path
            self._f = None
            self.path = None
        self._close_reader()
        keep_file = False
        try:
            if isinstance(view, memoryview):
                view.release()
            if mv is not None:
                mv.release()
            if mm is not None:
                mm.close()
        except BufferError:  # pragma: no cover - a caller kept an exported view
            log.warning("record set %s still has exported views; file kept until exit", p)
            keep_file = True
        except Exception:  # pragma: no cover - never raise from close/finalizers
            log.debug("closing record set mapping failed", exc_info=True)
        if f is not None:
            try:
                f.close()
            except OSError:  # pragma: no cover
                pass
        if p is not None and not keep_file:
            _remove_file(p)


# ----------------------------------------------------------------------------------
# RecordSet
# ----------------------------------------------------------------------------------
class RecordSet:
    """Ordered record indices (spec §6; see the module docstring).

    * ``kind``: ``"base"``, ``"filter"``, ``"sort"``, ``"search"``, ``"manual"``, ...;
      ``description``: localized text for the UI; ``report``: :class:`FilterReport` /
      :class:`SortReport` of the run that made the set (``None`` otherwise).
    * ``len()``, ``rs[i]`` (negative indices count from the end; slices give an
      ``array('Q')`` copy), iteration, :meth:`iter_chunks`, :meth:`to_list`.
    * ``complete``: the set is final (a builder finished it / the base set's index is
      complete); ``path``: the backing file (``None`` for in-memory and base sets).
    * ``gen``: the overlay-state gen its positions belong to (docs/ARCHITECTURE.md §12.3; a
      plain, assignable attribute; ``None`` = valid for any state, e.g. the base set).
      Filter, sort and search results carry the gen of the pinned document they read;
      consumers taking a ``source`` refuse one of another state
      (``EditError("edits.error.stale_scope")``).
    * Context manager; :meth:`close` (= :meth:`delete`) releases and removes the file.
    """

    def __init__(self, store: _Storage | _BaseStore, *, kind: str, description: str = "",
                 report: Any = None, gen: int | None = None) -> None:
        self._store = store
        self.kind = kind
        self.description = description
        self.report = report
        self.gen: int | None = gen
        self._finalizer = weakref.finalize(self, store.close)

    # -- construction ----------------------------------------------------------------
    @classmethod
    def base(cls, doc: Any) -> RecordSet:
        """All records of ``doc`` in order (virtual identity; ``len()`` is
        ``doc.record_count``, live while the index grows -- a pinned view gives the frozen
        count of its state). ``gen`` is ``None``: "all records" of any state."""
        return cls(_BaseStore(doc), kind="base", description=t("recordset.desc.base"))

    @classmethod
    def from_indices(cls, indices: Iterable[int], kind: str = "manual", description: str = "", *,
                     gen: int | None = None) -> RecordSet:
        """A complete set holding ``indices`` (list, tuple, ``array``, ``range``,
        RecordSet or any iterable of non-negative ints) in the given order."""
        builder = RecordSetBuilder(kind, description, gen=gen)
        try:
            builder.extend(indices)
            return builder.finish()
        except BaseException:
            builder.abort()
            raise

    # -- properties --------------------------------------------------------------------
    @property
    def complete(self) -> bool:
        return self._store.complete

    @property
    def closed(self) -> bool:
        return self._store.state == "closed"

    @property
    def path(self) -> Path | None:
        """The backing ``.u64`` file, ``None`` when the set is held in memory/virtual."""
        return self._store.path

    def is_ascending(self) -> bool:
        """Indices strictly increasing (so far, for a growing set)."""
        return bool(self._store.ascending)

    def _is_base_of(self, doc: Any) -> bool:
        """The base set of ``doc`` or of another view of the same live document (origins are
        compared: the base set of the live document means "all records" for any pinned state)."""
        store = self._store
        if not isinstance(store, _BaseStore):
            return False
        mine = store.doc
        if mine is None:
            return False
        return mine is doc or getattr(mine, "origin", mine) is getattr(doc, "origin", doc)

    # -- access ------------------------------------------------------------------------
    def __len__(self) -> int:
        return self._store.count()

    def __getitem__(self, i: int | slice) -> Any:
        if isinstance(i, slice):
            start, stop, step = i.indices(len(self))
            if step == 1:
                return self._store.get_many(start, max(0, stop - start))
            get = self._store.get
            return array("Q", [get(k) for k in range(start, stop, step)])
        i = operator.index(i)
        n = len(self)
        if i < 0:
            i += n
        if i < 0 or i >= n:
            raise IndexError(f"record set index out of range ({n} records)")
        return self._store.get(i)

    def __iter__(self) -> Iterator[int]:
        for chunk in self.iter_chunks():
            yield from chunk

    def iter_chunks(self, size: int = CHUNK_ITEMS) -> Iterator[array]:
        """The indices as ``array('Q')`` chunks of at most ``size``. A growing set is read
        as far as it has grown when the iteration gets there (it never waits)."""
        size = max(1, int(size))
        pos = 0
        get_many = self._store.get_many
        while True:
            n = len(self)
            if pos >= n:
                return
            chunk = get_many(pos, min(size, n - pos))
            if not chunk:
                return
            pos += len(chunk)
            yield chunk

    def to_list(self, limit: int | None = None) -> list[int]:
        """The first ``limit`` indices (all when ``None``) as a list."""
        n = len(self) if limit is None else min(len(self), max(0, int(limit)))
        out: list[int] = []
        get_many = self._store.get_many
        pos = 0
        while pos < n:
            chunk = get_many(pos, min(CHUNK_ITEMS, n - pos))
            if not chunk:
                break
            out.extend(chunk)
            pos += len(chunk)
        return out

    def _lookup_many(self, positions: array) -> array:
        """``array('Q', [self[p] for p in positions])`` (positions in range; fast for
        complete sets)."""
        store = self._store
        if isinstance(store, _BaseStore):
            return positions
        view = store.view()
        if view is not None:
            return array("Q", map(view.__getitem__, positions))
        return array("Q", map(store.get, positions))

    # -- lifetime ----------------------------------------------------------------------
    def close(self) -> None:
        """Release the memory map / file handles and remove the backing file
        (idempotent). The set then reads as empty."""
        self._finalizer()

    def delete(self) -> None:
        """Same as :meth:`close` (record sets are temporary)."""
        self._finalizer()

    def __enter__(self) -> RecordSet:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self.closed else "complete" if self.complete else "growing"
        desc = f" {self.description!r}" if self.description else ""
        gen = f" gen={self.gen}" if self.gen is not None else ""
        return f"<RecordSet {self.kind}{desc} {len(self)} records {state}{gen}>"


class RecordSetBuilder:
    """Writes a :class:`RecordSet` (spec §6): ``append(i)`` / ``extend(indices)`` /
    ``finish()`` / ``abort()``.

    ``recordset`` is the set being built: it can be handed to readers in other threads
    at once (live ``len()``/``[]``) and :meth:`finish` returns the same object, complete.
    At most ``flush_bytes`` of indices are kept in memory (default ~1 MiB); complete sets
    up to ``memory_max`` bytes are kept in RAM, larger ones are memory-mapped. Used as a
    context manager the builder is aborted when the block ends without :meth:`finish`.
    """

    def __init__(self, kind: str = "manual", description: str = "", *, directory: StrPath | None = None,
                 flush_bytes: int | None = None, memory_max: int | None = None, gen: int | None = None) -> None:
        _purge_once()
        self._storage = _Storage(directory=directory, flush_bytes=flush_bytes, memory_max=memory_max)
        self.recordset = RecordSet(self._storage, kind=kind, description=description, gen=gen)

    @property
    def state(self) -> str:
        """``open``, ``complete`` (finished) or ``closed`` (aborted)."""
        return self._storage.state

    def __len__(self) -> int:
        return self._storage.count()

    def append(self, i: int) -> None:
        """Append one record index (a non-negative int)."""
        self._storage.append(operator.index(i))

    def extend(self, indices: Iterable[int]) -> None:
        """Append many indices: an ``array('Q')`` (fastest), ``range``, list, tuple,
        RecordSet or any iterable of ints (consumed in batches)."""
        st = self._storage
        if isinstance(indices, RecordSet):
            for chunk in indices.iter_chunks():
                st.extend(chunk)
            return
        if isinstance(indices, (array, range, list, tuple)):
            st.extend(_index_array(indices))
            return
        if isinstance(indices, (str, bytes, bytearray, memoryview)):
            raise TypeError(f"record indices must be integers, not {type(indices).__name__}")
        it = iter(indices)
        while True:
            batch = list(itertools.islice(it, CHUNK_ITEMS))
            if not batch:
                return
            st.extend(_index_array(batch))

    def finish(self) -> RecordSet:
        """Complete the set and return it (``builder.recordset``)."""
        self._storage.finish()
        return self.recordset

    def abort(self) -> None:
        """Discard the set (temporary file removed; the set reads as empty). A finished
        set is left alone. Idempotent."""
        if self._storage.state == "open":
            self.recordset.close()

    def __enter__(self) -> RecordSetBuilder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.abort()

    def __repr__(self) -> str:
        return f"<RecordSetBuilder {self.recordset.kind} {len(self)} records {self.state}>"


# ----------------------------------------------------------------------------------
# reports
# ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class FilterReport:
    """Statistics of :func:`filter_records`.

    ``scanned``: records examined; ``matched``: records kept; ``skipped_huge``: huge
    records the filter could not evaluate (JMESPath or ``$``); ``unparseable``: records
    that could not be decoded; ``strategy``: ``"extract"`` or ``"parse"``; ``elapsed``:
    seconds; ``bytes_read``: record bytes read; ``eval_errors``: records on which the
    expression failed (JMESPath runtime errors; treated as not matching),
    ``first_error_record`` / ``first_error``: the first of them and its localized
    reason; ``too_large``: huge records whose values would exceed the memory budget
    (skipped), ``first_too_large``: the first of them; ``warnings``: i18n keys (texts:
    :meth:`warning_texts`).
    """

    scanned: int
    matched: int
    skipped_huge: int
    unparseable: int
    strategy: str
    elapsed: float
    bytes_read: int = 0
    warnings: tuple[str, ...] = ()
    warning_params: tuple[dict[str, Any], ...] = ()
    eval_errors: int = 0
    first_error_record: int | None = None
    first_error: str = ""
    too_large: int = 0
    first_too_large: int | None = None

    def warning_texts(self) -> list[str]:
        """The warnings in the current UI language (:func:`_warning_texts`)."""
        return _warning_texts(self.warnings, self.warning_params)


@dataclass(frozen=True)
class SortReport:
    """Statistics of :func:`sort_records`.

    ``records``: records sorted; ``runs``: sorted runs written to disk (0 = in-memory
    sort); ``elapsed``: seconds; ``peak_bytes_estimate``: largest estimated size of the
    in-memory keys; ``strategy``: ``"memory"`` or ``"external"``; ``bytes_read``: record
    bytes read; ``key_bytes``: total size of the composite keys; ``merge_passes``:
    intermediate merge passes; ``approximate``: huge records whose key was built from a
    prefix of the value; ``missing``: per sort key, the records without a value;
    ``memory_limit``: the in-memory key limit used (bytes; the run size it grew to for a
    tiny limit); ``warnings``: i18n keys (texts: :meth:`warning_texts`).
    """

    records: int
    runs: int
    elapsed: float
    peak_bytes_estimate: int
    strategy: str
    bytes_read: int = 0
    key_bytes: int = 0
    merge_passes: int = 0
    approximate: int = 0
    warnings: tuple[str, ...] = ()
    warning_params: tuple[dict[str, Any], ...] = ()
    missing: tuple[int, ...] = ()
    memory_limit: int = 0

    def warning_texts(self) -> list[str]:
        """The warnings in the current UI language (:func:`_warning_texts`)."""
        return _warning_texts(self.warnings, self.warning_params)


def _count_text(n: int) -> str:
    """A count with grouped digits: ``12 345`` (Uzbek, Russian), ``12,345`` (English)."""
    text = f"{int(n):,}"
    return text.replace(",", " ") if space_grouped() else text


_COUNT_PARAMS = ("count", "total", "found")          # shown with grouped digits
_PLURAL_PARAM = {"recordset.warn.sort_key_absent": "total"}   # the count its noun goes with (else "count")


def _warning_texts(keys: Sequence[str], params: Sequence[dict[str, Any]]) -> list[str]:
    """Localized warning texts: ``<key>.one`` when the count the noun goes with is 1 and
    that key exists (English "1 record" / "2 records"; the Uzbek texts are the same), counts
    with grouped digits."""
    out = []
    for i, key in enumerate(keys):
        p = dict(params[i]) if i < len(params) else {}
        n = p.get(_PLURAL_PARAM.get(key, "count"))
        if isinstance(n, int) and not isinstance(n, bool) and n == 1 and has(key + ".one"):
            key += ".one"
        for name in _COUNT_PARAMS:
            v = p.get(name)
            if isinstance(v, int) and not isinstance(v, bool):
                p[name] = _count_text(v)
        out.append(t(key, **p))
    return out


# ----------------------------------------------------------------------------------
# shared runner helpers
# ----------------------------------------------------------------------------------
def _normalize_source(doc: Any, source: Any) -> Any:
    """``None`` for "all records" (also the doc's base set), else ``source``."""
    if source is None:
        return None
    if isinstance(source, RecordSet) and source._is_base_of(doc):
        return None
    return source


def _open_data(doc: Any) -> Any:
    """``doc.open_data()``: a reader of the document's virtual address space (an edited record's
    span is in the edit layer); a document-like object without it is read from ``data_path``."""
    open_data = getattr(doc, "open_data", None)
    if open_data is not None:
        return open_data()
    return open_shared(doc.data_path, "rb")


def _pin(doc: Any) -> Any:
    """``doc.pinned()`` (docs/ARCHITECTURE.md §12.3): a run reads ONE overlay state; a
    document-like object without ``pinned`` is used as it is."""
    pinned = getattr(doc, "pinned", None)
    return pinned() if callable(pinned) else doc


def _check_scope(doc: Any, source: Any) -> None:
    """``EditError("edits.error.stale_scope")`` for a source made for another overlay state
    (``source.gen`` set and not ``doc.gen``): its positions address other records."""
    gen = getattr(source, "gen", None) if source is not None else None
    if gen is not None and gen != getattr(doc, "gen", 0):
        raise EditError("edits.error.stale_scope")


def _estimate_total(doc: Any, source: Any) -> int | None:
    """Expected record bytes of ``source`` (exact for all records, else proportional; an
    edited document: from ``doc.effective_bytes()``)."""
    if getattr(doc, "edited", False):
        full = max(0, int(doc.effective_bytes()))
    else:
        full = max(0, int(doc.data_size) - int(doc.info.data_start))
    if source is None:
        return full
    try:
        n = len(source)
    except TypeError:
        return None
    count = doc.record_count
    if count <= 0:
        return None
    return int(full * min(1.0, n / count))


class _Tracker:
    """Byte progress of a runner (sets a total only when the caller did not).

    :meth:`add` reports record bytes read (``done``). A sort reads the records and then
    merges their keys, whose size is known only once everything was read; so that its bar
    never goes back, the reading phase is reported with :meth:`add_keys`: the key bytes
    collected so far, projected onto the whole input, are part of the total from the first
    block on (the total is set once, while nothing is shown yet, and then stays fixed), the
    reading phase fills its projected share of the bar (never going back) and the merge
    phase (:meth:`phase`, then :meth:`add`) fills the rest in proportion to the key bytes
    merged. The value shown never exceeds the total, and it reaches it only when the last
    keys are merged. Without a size estimate (no total) the raw byte counts are shown.
    """

    __slots__ = ("progress", "done", "own_total", "estimate", "_shown", "_planned", "_from", "_size", "_merged")

    def __init__(self, progress: Progress | None, total: int | None, message: str) -> None:
        self.progress = progress
        self.done = 0                   # record bytes read
        self.own_total = False
        self.estimate = total           # expected record bytes (None: unknown)
        self._shown = 0                 # the value last reported
        self._planned = False           # sort: the merge is part of the total
        self._from: int | None = None   # sort: where the merge phase started on the bar
        self._size = 0                  # sort: key bytes to merge
        self._merged = 0
        if progress is not None:
            if not progress.message:
                progress.set_message(message)
            if progress.total is None and total is not None:
                progress.set_total(total)
                self.own_total = True

    def _report(self, value: int) -> None:
        self._shown = value
        self.progress.update(value)  # type: ignore[union-attr]

    def add(self, n: int) -> None:
        """``n`` more bytes: record bytes read, or -- after :meth:`phase` -- key bytes merged."""
        if self._from is None:
            self.done += n
            if self.progress is not None:
                self._report(self.done)
            return
        self._merged += n
        p = self.progress
        if p is None:
            return
        total = p.total
        if not self._planned or not total or self._size <= 0:
            self._report(self._from + self._merged)
            return
        frac = min(1.0, self._merged / self._size)
        self._report(max(self._shown, min(total, self._from + int((total - self._from) * frac))))

    def add_keys(self, n: int, key_bytes: int) -> None:
        """Sort, reading phase: ``n`` more record bytes read; ``key_bytes``: the size of the
        keys collected so far (class docstring)."""
        self.done += n
        p = self.progress
        if p is None:
            return
        done = self.done
        if not self.estimate or p.total is None or done <= 0:
            self._report(done)  # unknown size: raw bytes
            return
        span = max(self.estimate, done)
        projected = key_bytes * span / done        # key bytes of the whole input
        if not self._planned:
            self._planned = True
            if self.own_total:
                p.set_total(max(1, int(self.estimate + projected)))  # nothing is shown yet
        total = int(p.total or 0)
        value = int(total * done / (span + projected)) if span + projected > 0 else 0
        self._report(max(0, min(max(self._shown, value), total - 1)))  # the merge is still to come

    def phase(self, extra: int, message: str) -> None:
        """Start a phase of ``extra`` more bytes (sort: merging the keys)."""
        self._from = self._shown if self._planned else self.done
        self._size = extra
        self._merged = 0
        p = self.progress
        if p is None:
            return
        if self.own_total and not self._planned:
            p.set_total(self.done + extra)
        p.set_message(message)

    def finish(self) -> None:
        if self.progress is not None:
            self.progress.finish()


def _huge_warning_params(doc: Any, count: int) -> dict[str, Any]:
    return {"count": count, "size": format_bytes(doc.settings.large_node_bytes)}


def _ensure_unchanged(doc: Any) -> None:
    """``doc.ensure_unchanged()`` (FileChangedError when another program changed the file
    while it was being read)."""
    ensure = getattr(doc, "ensure_unchanged", None)
    if ensure is not None:
        ensure()


# ----------------------------------------------------------------------------------
# filter
# ----------------------------------------------------------------------------------
def _decode_value(raw: bytes) -> Any:
    """``codec.loads(raw)`` for one extracted (valid) JSON value, with identical results
    and fast paths: strings without escapes, literals, integers of at most 18 characters
    (``int``) and float tokens (``float`` when ``codec.is_plain_float_token``, else
    ``codec.JsonFloat`` -- the codec's documented number rule)."""
    c = raw[0]
    if c == 0x22:  # string
        if 0x5C not in raw:
            return raw[1:-1].decode("utf-8")
        return codec.loads(raw)
    if c == 0x74:
        return True
    if c == 0x66:
        return False
    if c == 0x6E:
        return None
    if c == 0x7B or c == 0x5B:
        return codec.loads(raw)
    if len(raw) <= 18 and raw != b"-0" and (raw.isdigit() or (c == 0x2D and raw[1:].isdigit())):
        return int(raw)
    if b"." in raw or b"e" in raw or b"E" in raw:
        tok = raw.decode("ascii")
        return float(tok) if codec.is_plain_float_token(tok) else codec.JsonFloat(tok)
    return codec.loads(raw)  # long integers, -0


class _FilterRunner:
    """Evaluation state of one :func:`filter_records` call."""

    def __init__(self, doc: Any, flt: Filter, strategy: str, cancel: CancelToken | None = None) -> None:
        self.doc = doc
        self.flt = flt
        self.cancel = cancel
        settings = getattr(doc, "settings", None) or get_settings()
        self.max_bytes = capture_budget(settings)
        pats = flt.required_patterns
        self.jmes = pats is None
        self.patterns: tuple[PathPattern, ...] = tuple(pats or ())
        self.root = any(not p.segments for p in self.patterns)
        extractable = not self.jmes and bool(flt.extractable) and not self.root
        if strategy == "auto":
            # the pure-Python reference extractor validates every record with regular
            # expressions and is slower than codec.loads (measured ~19 vs ~48 MB/s)
            strategy = "extract" if extractable and core.BACKEND == "rust" else "parse"
        elif strategy == "extract":
            if not extractable:
                log.info("filter %r cannot use the extract strategy; parsing records", flt.text)
                strategy = "parse"
        elif strategy != "parse":
            raise ValueError(f"unknown filter strategy {strategy!r}; expected auto, extract or parse")
        self.strategy = strategy
        self.need_key = bool(flt.uses_key)
        self.need_value = self.jmes or bool(self.patterns)
        # values for Filter.matches_values are keyed by pattern *position*: int keys hash
        # in C (PathPattern.__hash__ is a Python method)
        self.empty: dict[int, tuple] = {i: () for i in range(len(self.patterns))}
        self.member = False
        self.extractor: Any = None
        self.started = False
        self.scanned = 0
        self.skipped_huge = 0
        self.unparseable = 0
        self.eval_errors = 0
        self.first_error_record: int | None = None
        self.first_error = ""
        self.first_error_detail = ""
        self.any_success = False   # the expression was evaluated on some record without an error
        self.too_large = 0
        self.first_too_large: int | None = None

    def start(self) -> None:
        """Called at the first block: the member flag of wrapped documents is known once
        the first record is indexed."""
        self.started = True
        self.member = bool(self.doc.member_records)
        if self.strategy == "extract" and self.patterns:
            self.extractor = core.PathExtractor([p.to_core() for p in self.patterns], member_spans=self.member)

    # -- blocks ------------------------------------------------------------------------
    def block(self, blk: Block, out: list[int]) -> None:
        self.scanned += len(blk.indices)
        if self.strategy == "extract":
            self._block_extract(blk, out, single=False)
        else:
            self._block_parse(blk, out)

    def _key_of(self, data: bytes, s: int, e: int) -> str | None:
        return split_member(data[s:e])[0] if self.member else None

    def _block_extract(self, blk: Block, out: list[int], single: bool) -> None:
        data = blk.data
        idxs = blk.indices
        n = len(idxs)
        sp = _u64(blk.spans)
        matches_values = self.flt.matches_values
        need_key = self.need_key
        key = None
        if not self.patterns:
            empty = self.empty
            for k in range(n):
                if need_key:
                    key = self._key_of(data, sp[2 * k], sp[2 * k + 1])
                if matches_values(empty, key=key, index=idxs[k]):
                    out.append(idxs[k])
            if n:
                self.any_success = True
            return
        try:
            quads = self.extractor.extract(data, blk.spans)
        except ValueError:
            if single:
                self._eval_streamed(int(idxs[0]), out)
                return
            self._block_extract_each(blk, sp, out)
            return
        q = _u64(quads)
        nq = len(q) // 4
        sidx = q[0::4]
        pidx = q[1::4]
        raws = list(map(data.__getitem__, map(slice, q[2::4], q[3::4])))
        try:
            decoded: list[Any] = list(map(_decode_value, raws))
            bad: set[int] = set()
        except _DECODE_ERRORS:
            decoded, bad = self._decode_each(raws, sidx)
        npat = len(self.patterns)
        empty = self.empty
        j = 0
        for k in range(n):
            idx = idxs[k]
            if j < nq and sidx[j] == k:
                if k in bad:
                    while j < nq and sidx[j] == k:
                        j += 1
                    self._eval_streamed(idx, out)
                    continue
                vals: dict[int, Any] = {}
                while j < nq and sidx[j] == k:
                    lst = vals.get(pidx[j])
                    if lst is None:
                        vals[pidx[j]] = [decoded[j]]
                    else:
                        lst.append(decoded[j])
                    j += 1
                if len(vals) < npat:
                    for pi in range(npat):
                        if pi not in vals:
                            vals[pi] = ()
            else:
                vals = empty
            if need_key:
                key = self._key_of(data, sp[2 * k], sp[2 * k + 1])
            if matches_values(vals, key=key, index=idx):
                out.append(idx)
        if n > len(bad):  # some records were evaluated here (the others were streamed)
            self.any_success = True

    @staticmethod
    def _decode_each(raws: list[bytes], sidx: array) -> tuple[list[Any], set[int]]:
        """Decode value by value; returns the values and the records (block positions)
        holding a value that cannot be decoded in memory."""
        decoded: list[Any] = []
        bad: set[int] = set()
        for j, raw in enumerate(raws):
            try:
                decoded.append(_decode_value(raw))
            except _DECODE_ERRORS:
                decoded.append(None)
                bad.add(sidx[j])
        return decoded, bad

    def _block_extract_each(self, blk: Block, sp: array, out: list[int]) -> None:
        """The block holds a record the extractor rejects: evaluate record by record."""
        for k, idx in enumerate(blk.indices):
            one = Block(blk.data, array("Q", (idx,)), sp[2 * k:2 * k + 2].tobytes(), False, False)
            self._block_extract(one, out, single=True)

    def _block_parse(self, blk: Block, out: list[int]) -> None:
        data = blk.data
        idxs = blk.indices
        sp = _u64(blk.spans)
        loads = codec.loads
        matches = self.flt.matches
        member = self.member
        need_value = self.need_value
        split = member and (need_value or self.need_key)
        key = None
        value = None
        succeeded = self.any_success
        for k in range(len(idxs)):
            idx = idxs[k]
            buf = data[sp[2 * k]:sp[2 * k + 1]]
            try:
                if split:
                    key, vo = split_member(buf)
                    if need_value:
                        value = loads(buf[vo:])
                elif need_value:
                    value = loads(buf)
            except _DECODE_ERRORS:
                self._eval_streamed(idx, out)
                succeeded = self.any_success
                continue
            try:
                ok = matches(value, key=key, index=idx)
            except FilterEvalError as exc:
                self._eval_error(idx, exc)
                continue
            if ok:
                out.append(idx)
            if not succeeded:
                succeeded = self.any_success = True

    # -- streamed evaluation -----------------------------------------------------------
    def _eval_streamed(self, idx: int, out: list[int], span: tuple[int, int] | None = None) -> None:
        """Evaluate a record by streaming it with ijson (a huge record, or one that could
        not be decoded in memory). Counted as unparseable when streaming fails as well,
        as too large when its values exceed the capture budget."""
        try:
            if span is None:
                span = self.doc.record_span(idx)
            key = self.doc.record_key(idx) if self.member and self.need_key else None
            ok = self.flt.matches_stream(self.doc, span, self.member, key=key, index=idx, max_bytes=self.max_bytes,
                                         cancel=self.cancel)
        except CaptureLimitExceeded as exc:
            self._too_large(idx, exc)
            return
        except FilterEvalError as exc:
            self._eval_error(idx, exc)
            return
        except JsonReshapeError as exc:
            if isinstance(exc, Cancelled) or exc.key != "stream.error.parse":
                raise
            self._unparseable(idx, exc)
            return
        except (ValueError, RecursionError) as exc:
            self._unparseable(idx, exc)
            return
        self.any_success = True
        if ok:
            out.append(idx)

    def _unparseable(self, idx: int, exc: BaseException) -> None:
        self.unparseable += 1
        log.warning("record #%d cannot be decoded and was skipped by the filter: %s", idx, exc)

    def _eval_error(self, idx: int, exc: FilterEvalError) -> None:
        """The expression failed on this record: it does not match (counted). When it has
        failed on the first :data:`ALL_FAILED_LIMIT` records it was evaluated on, the filter
        stops (:meth:`all_failed_error`)."""
        self.eval_errors += 1
        if self.first_error_record is None:
            self.first_error_record = int(idx)
            self.first_error = exc.message
            self.first_error_detail = str(exc.params.get("detail", ""))
        if self.eval_errors <= 20:
            log.warning("filter %r cannot be evaluated on record #%d: %s (%s)", self.flt.text, idx, exc.message,
                        exc.params.get("detail", ""))
        if not self.any_success and self.eval_errors >= ALL_FAILED_LIMIT:
            raise self.all_failed_error()

    def all_failed_error(self) -> UserInputError:
        """The expression could not be evaluated on any record: a filter result would only
        be empty (the user's expression is wrong, e.g. a function given a field of another
        type), so the filter stops with this localized error naming the first failure."""
        log.warning("filter %r could not be evaluated on any of the %d records it was evaluated on; stopping",
                    self.flt.text, self.eval_errors)
        return UserInputError("recordset.error.eval_failed", count=_count_text(self.eval_errors),
                              record=self.first_error_record, error=self.first_error,
                              detail=self.first_error_detail)

    def _too_large(self, idx: int, exc: CaptureLimitExceeded) -> None:
        self.too_large += 1
        if self.first_too_large is None:
            self.first_too_large = int(idx)
        log.warning("record #%d is too large to evaluate the filter %r in memory: %s", idx, self.flt.text, exc)

    def huge(self, blk: Block, out: list[int]) -> None:
        """A record larger than ``large_node_bytes``: streamed with ijson."""
        self.scanned += 1
        idx = int(blk.indices[0])
        if self.jmes or self.flt.stream_captures_root:
            self.skipped_huge += 1
            return
        self._eval_streamed(idx, out, blk.span(0))


def filter_records(doc: Any, flt: Filter | str, source: Any = None, *, progress: Progress | None = None,
                   cancel: CancelToken | None = None, on_batch: Callable[[RecordSet], Any] | None = None,
                   strategy: str = "auto", block_bytes: int = BLOCK_BYTES,
                   batch_interval: float = BATCH_INTERVAL) -> RecordSet:
    """Records of ``source`` matched by ``flt``, in source order (spec §6, §9).

    ``flt``: a compiled :class:`~jsonreshape.query.Filter` (or expression text);
    ``source``: ``None`` (all records), a RecordSet, a ``range``, a list or any iterable
    of record indices; ``strategy``: ``"auto"``, ``"extract"`` (falls back to
    ``"parse"`` when the filter needs whole records) or ``"parse"``;
    ``on_batch(recordset)``: called with the growing result at most every
    ``batch_interval`` seconds. The result's ``report`` is a :class:`FilterReport`.
    Raises :class:`~jsonreshape.errors.Cancelled` (nothing left on disk) when cancelled.
    """
    if isinstance(flt, str):
        flt = compile_filter(flt)
    if not isinstance(flt, Filter):
        raise TypeError(f"expected a Filter, not {type(flt).__name__}")
    doc = _pin(doc)
    _check_scope(doc, source)
    started = time.perf_counter()
    runner = _FilterRunner(doc, flt, strategy, cancel)
    src = _normalize_source(doc, source)
    tracker = _Tracker(progress, _estimate_total(doc, src), t("recordset.progress.filter", name=doc.source_path.name))
    builder = RecordSetBuilder("filter", t("recordset.desc.filter", expr=flt.text), gen=getattr(doc, "gen", None))
    rs = builder.recordset
    out: list[int] = []
    last_batch = time.monotonic()
    last_len = 0
    try:
        for blk in doc.iter_blocks(src, block_bytes=block_bytes, cancel=cancel):
            if not runner.started:
                runner.start()
            if blk.huge:
                runner.huge(blk, out)
                s, e = blk.span(0)
                tracker.add(e - s)
            else:
                runner.block(blk, out)
                tracker.add(len(blk.data))
            if out:
                builder.extend(out)
                out.clear()
            check(cancel)
            if on_batch is not None:
                now = time.monotonic()
                n = len(builder)
                if n != last_len and now - last_batch >= batch_interval:
                    last_batch = now
                    last_len = n
                    on_batch(rs)
                    check(cancel)
        _ensure_unchanged(doc)
        if runner.eval_errors and not runner.any_success:
            raise runner.all_failed_error()
        rs = builder.finish()
    except BaseException:
        builder.abort()
        raise
    tracker.finish()
    warnings: list[str] = []
    params: list[dict[str, Any]] = []
    if runner.skipped_huge:
        warnings.append("recordset.warn.skipped_huge")
        params.append(_huge_warning_params(doc, runner.skipped_huge))
        log.warning("filter %r: %d huge records skipped (JMESPath or '$' needs the whole record)", flt.text,
                    runner.skipped_huge)
    if runner.too_large:
        warnings.append("recordset.warn.too_large")
        params.append({"count": runner.too_large, "record": runner.first_too_large,
                       "size": format_bytes(runner.max_bytes)})
    if runner.eval_errors:
        warnings.append("recordset.warn.eval_errors")
        params.append({"count": runner.eval_errors, "record": runner.first_error_record, "error": runner.first_error})
        log.warning("filter %r could not be evaluated on %d records (first: #%s)", flt.text, runner.eval_errors,
                    runner.first_error_record)
    if runner.unparseable:
        warnings.append("recordset.warn.unparseable")
        params.append({"count": runner.unparseable})
    elapsed = time.perf_counter() - started
    rs.report = FilterReport(scanned=runner.scanned, matched=len(rs), skipped_huge=runner.skipped_huge,
                             unparseable=runner.unparseable, strategy=runner.strategy, elapsed=elapsed,
                             bytes_read=tracker.done, warnings=tuple(warnings), warning_params=tuple(params),
                             eval_errors=runner.eval_errors, first_error_record=runner.first_error_record,
                             first_error=runner.first_error, too_large=runner.too_large,
                             first_too_large=runner.first_too_large)
    log.info("filter %r on %s: %d of %d records in %.2f s (%s, %.0f MB/s)", flt.text, doc.source_path.name, len(rs),
             runner.scanned, elapsed, runner.strategy, tracker.done / elapsed / 1e6 if elapsed > 0 else 0.0)
    return rs


# ----------------------------------------------------------------------------------
# sort: key collection
# ----------------------------------------------------------------------------------
class _KeyCollector:
    """Builds the composite keys ``field keys + seq (8 bytes BE)`` of the records."""

    def __init__(self, doc: Any, specs: list[SortKey], cancel: CancelToken | None) -> None:
        self.doc = doc
        self.specs = specs
        self.cancel = cancel
        self.value_pos = [i for i, s in enumerate(specs) if s.kind == "value"]
        self.tok = [token_encoder(s) for s in specs]
        self.val = [value_encoder(s) for s in specs]
        self.missing = [enc(None) for enc in self.tok]
        self.concrete = [s.kind == "value" and s.pattern.is_concrete for s in specs]  # type: ignore[union-attr]
        self.member = False
        self.extractor: Any = None
        self.started = False
        self.approximate = 0
        self.first_approximate: int | None = None
        self.missing_counts = [0] * len(specs)   # records without a value, per key
        settings = getattr(doc, "settings", None) or get_settings()
        self.max_bytes = capture_budget(settings)

    def start(self) -> None:
        self.started = True
        self.member = bool(self.doc.member_records)
        paths = [self.specs[i].core_path() for i in self.value_pos]
        if paths:
            self.extractor = core.PathExtractor(paths, member_spans=self.member)

    # -- blocks ------------------------------------------------------------------------
    def block(self, blk: Block, seq0: int) -> list[bytes]:
        """Composite keys of the records of a regular block (sequence numbers from
        ``seq0``)."""
        try:
            return self._block(blk, seq0)
        except _DECODE_ERRORS:
            # a record the extractor or a token encoder rejects: key record by record
            return self._block_each(blk, seq0)

    def _block(self, blk: Block, seq0: int) -> list[bytes]:
        data = blk.data
        idxs = blk.indices
        n = len(idxs)
        specs = self.specs
        cols: list[list[bytes]] = [[] for _ in specs]
        missing_counts: dict[int, int] = {}   # applied at the end: a failing block is keyed again record by record
        if self.extractor is not None:
            q = _u64(self.extractor.extract(data, blk.spans))
            sidx = q[0::4]
            st = q[2::4]
            en = q[3::4]
            if len(self.value_pos) == 1:
                pos = self.value_pos[0]
                enc = self.tok[pos]
                if self.concrete[pos] and len(sidx) == n:
                    col = list(map(enc, map(data.__getitem__, map(slice, st, en))))
                    missing_counts[pos] = 0
                else:
                    col = [self.missing[pos]] * n
                    prev = -1
                    found = 0
                    for j in range(len(sidx)):
                        k = sidx[j]
                        if k != prev:
                            col[k] = enc(data[st[j]:en[j]])
                            prev = k
                            found += 1
                    missing_counts[pos] = n - found
                cols[pos] = col
            else:
                pidx = q[1::4]
                nv = len(self.value_pos)
                encs = [self.tok[pos] for pos in self.value_pos]
                tmp: list[list[Any]] = [[None] * n for _ in range(nv)]
                for j in range(len(sidx)):
                    vi = pidx[j]
                    c = tmp[vi]
                    k = sidx[j]
                    if c[k] is None:
                        c[k] = encs[vi](data[st[j]:en[j]])
                for vi, pos in enumerate(self.value_pos):
                    miss = self.missing[pos]
                    missing_counts[pos] = tmp[vi].count(None)
                    cols[pos] = [miss if x is None else x for x in tmp[vi]]
        keys: list[str | None] | None = None
        for pos, spec in enumerate(specs):
            kind = spec.kind
            if kind == "key":
                if keys is None:
                    if self.member:
                        sp = _u64(blk.spans)
                        keys = [split_member(data[sp[2 * k]:sp[2 * k + 1]])[0] for k in range(n)]
                    else:
                        keys = [None] * n
                venc = self.val[pos]
                cols[pos] = [venc(MISSING if kk is None else kk) for kk in keys]
                missing_counts[pos] = keys.count(None)
            elif kind == "index":
                cols[pos] = list(map(self.val[pos], idxs))
        seqs = map(_SEQ_PACK, range(seq0, seq0 + n))
        if len(specs) == 1:
            out = list(map(operator.add, cols[0], seqs))
        else:
            out = list(map(b"".join, zip(*cols, seqs)))
        for pos, count in missing_counts.items():
            self.missing_counts[pos] += count
        return out

    def _block_each(self, blk: Block, seq0: int) -> list[bytes]:
        sp = _u64(blk.spans)
        out: list[bytes] = []
        for k, idx in enumerate(blk.indices):
            one = Block(blk.data, array("Q", (idx,)), sp[2 * k:2 * k + 2].tobytes(), False, False)
            try:
                out.extend(self._block(one, seq0 + k))
            except _DECODE_ERRORS:
                out.append(self._streamed(int(idx), self.doc.record_span(int(idx)), seq0 + k, huge=False))
        return out

    def huge(self, blk: Block, seq: int) -> bytes:
        idx = int(blk.indices[0])
        return self._streamed(idx, blk.span(0), seq, huge=True)

    # -- streamed records --------------------------------------------------------------
    def _streamed(self, idx: int, span: tuple[int, int], seq: int, huge: bool) -> bytes:
        """Composite key of one record read with ijson (huge records, decode failures).
        Only the first value of each field is kept (``first_only``) within the capture
        budget; a field value larger than the budget is keyed by its beginning
        (:meth:`_prefix_key`, counted as approximate)."""
        specs = self.specs
        stream_pos: list[int] = []
        for pos in self.value_pos:
            if huge and not specs[pos].pattern.segments:  # type: ignore[union-attr]
                continue  # '$' of a huge record: prefix key below
            stream_pos.append(pos)
        values: dict[int, Any] = {}
        approximate = False
        if stream_pos:
            # (fallbacks run after the except blocks: a traceback keeps the failed pass's
            # values alive)
            too_large: list[int] = []
            try:
                got = stream_values(self.doc, span, [specs[pos].pattern for pos in stream_pos], member=self.member,
                                    max_bytes=self.max_bytes, first_only=True, cancel=self.cancel)
                values = {pos: got[k] for k, pos in enumerate(stream_pos)}
            except CaptureLimitExceeded:
                too_large = list(stream_pos)
            if len(too_large) > 1:  # field by field: only the too large ones use a prefix
                retry, too_large = too_large, []
                for pos in retry:
                    try:
                        values[pos] = stream_values(self.doc, span, [specs[pos].pattern], member=self.member,
                                                    max_bytes=self.max_bytes, first_only=True, cancel=self.cancel)[0]
                    except CaptureLimitExceeded:
                        too_large.append(pos)
            for pos in too_large:
                values[pos] = self._prefix_key(span, pos)
                approximate = True
        fields: list[bytes] = []
        for pos, spec in enumerate(specs):
            kind = spec.kind
            if kind == "value":
                if pos in values:
                    got_value = values[pos]
                    if isinstance(got_value, bytes):  # a prefix key
                        fields.append(got_value)
                    else:
                        if not got_value:
                            self.missing_counts[pos] += 1
                        fields.append(self.val[pos](got_value[0] if got_value else MISSING))
                else:
                    key_bytes, exact = self._huge_root_key(idx, span, pos)
                    fields.append(key_bytes)
                    approximate = approximate or not exact
            elif kind == "key":
                key = self.doc.record_key(idx) if self.member else None
                if key is None:
                    self.missing_counts[pos] += 1
                fields.append(self.val[pos](MISSING if key is None else key))
            else:
                fields.append(self.val[pos](idx))
        if approximate:
            self.approximate += 1
            if self.first_approximate is None:
                self.first_approximate = idx
        return b"".join(fields) + _SEQ_PACK(seq)

    def _prefix_key(self, span: tuple[int, int], pos: int) -> bytes:
        """Field key of a value too large to capture: a string by its first
        :data:`HUGE_KEY_PREFIX` characters, an object/array by the first
        :data:`HUGE_KEY_PREFIX` bytes of its compact JSON text (like ``$`` of a huge
        record)."""
        spec = self.specs[pos]
        got = stream_text_prefix(self.doc, span, spec.pattern or _ROOT, self.member,
                                 limit=HUGE_KEY_PREFIX, cancel=self.cancel)
        if got is None:  # pragma: no cover - the capture just saw a value
            return self.val[pos](MISSING)
        kind, data, complete = got
        if kind in ("string", "scalar"):
            return self.val[pos](data)
        payload = data + b"\x00" if complete else data
        tag = self.val[pos]({} if kind == "object" else [])[:1]
        return tag + (payload.translate(_INV) if spec.descending else payload)

    def _huge_root_key(self, idx: int, span: tuple[int, int], pos: int) -> tuple[bytes, bool]:
        """``(field key, exact)`` of ``$`` for a huge record: exact for scalars; a string
        or an object/array by its first :data:`HUGE_KEY_PREFIX` characters / bytes of
        compact JSON text (module docstring)."""
        spec = self.specs[pos]
        vs, ve = self.doc.record_value_span(idx) if self.member else span
        with _open_data(self.doc) as f:   # an edited record lives in the edit layer
            first = read_range(f, vs, vs + 1) if ve > vs else b""
            if first not in (b"{", b"["):
                vals: list[Any] | None
                try:
                    vals = stream_values(self.doc, span, [_ROOT], member=self.member, max_bytes=self.max_bytes,
                                         cancel=self.cancel)[0]
                except CaptureLimitExceeded:
                    vals = None
                if vals is None:  # a string too large to key whole: its beginning
                    return self._prefix_key(span, pos), False
                return self.val[pos](vals[0] if vals else MISSING), True
            fmt = core.Reformatter(None)
            out = bytearray()
            p = vs
            while p < ve and len(out) <= HUGE_KEY_PREFIX:
                check(self.cancel)
                stop = min(ve, p + HUGE_READ_BYTES)
                out += fmt.feed(read_range(f, p, stop))
                p = stop
            if p >= ve:
                out += fmt.finish()
        complete = p >= ve and len(out) <= HUGE_KEY_PREFIX
        payload = bytes(out) + b"\x00" if complete else bytes(out[:HUGE_KEY_PREFIX])
        tag = self.val[pos]({} if first == b"{" else [])[:1]
        return tag + (payload.translate(_INV) if spec.descending else payload), complete


# ----------------------------------------------------------------------------------
# sort: in-memory / external sorting
# ----------------------------------------------------------------------------------
def _write_run(f: Any, entries: Iterable[bytes], cancel: CancelToken | None = None) -> int:
    """Write length-prefixed keys to the binary file ``f``; returns the bytes written.
    Cancellation is checked every :data:`RUN_WRITE_ITEMS` keys."""
    pack = _LEN.pack
    total = 0
    it = iter(entries)
    while True:
        check(cancel)
        chunk = list(itertools.islice(it, RUN_WRITE_ITEMS))   # small: one join holds the GIL
        if not chunk:
            return total
        data = b"".join(itertools.chain.from_iterable(zip(map(pack, map(len, chunk)), chunk)))
        f.write(data)
        total += len(data)


def _iter_run(f: Any, bufsize: int) -> Iterator[bytes]:
    """Keys of a run file opened in binary mode, in order."""
    unpack_from = _LEN.unpack_from
    buf = b""
    pos = 0
    while True:
        if len(buf) - pos < 4:
            more = f.read(bufsize)
            if not more:
                if pos != len(buf):
                    raise ValueError("truncated sort run file")
                return
            buf = buf[pos:] + more
            pos = 0
            continue
        n = unpack_from(buf, pos)[0]
        end = pos + 4 + n
        if end > len(buf):
            more = f.read(max(bufsize, end - len(buf)))
            if not more:
                raise ValueError("truncated sort run file")
            buf = buf[pos:] + more
            pos = 0
            continue
        yield buf[pos + 4:end]
        pos = end


def _merge_runs(runs: list[list[bytes]], size: int, release: bool = False,
                cancel: CancelToken | None = None) -> Iterator[list[bytes]]:
    """Sorted batches of the merge of the sorted lists ``runs`` (a few). The pivot is the
    smallest of the runs' ``size``-th next keys; every run gives its keys up to the pivot
    (``bisect``): the run that set the pivot gives ``size`` keys, so every batch but the
    last has at least ``size`` keys -- whatever the key distribution -- and at most
    ``len(runs) * size``; its ``list.sort`` (a few sorted runs: Timsort merges them in
    linear time) is short. ``release``: the used positions of the runs are set to ``None``,
    so the keys die with the batches the caller drops (not all at once at the end).
    Cancellation is checked before every batch; when the merge ends early (cancelled, or
    closed by the consumer) the runs are emptied at once -- an exception's traceback keeps
    this frame, and so the lists, alive until the exception is dropped."""
    bisect_right = bisect.bisect_right
    pos = [0] * len(runs)
    lens = [len(r) for r in runs]
    live = [j for j in range(len(runs)) if lens[j]]
    batch: list[bytes] = []
    try:
        while len(live) > 1:
            check(cancel)
            pivot = min(runs[j][min(pos[j] + size, lens[j]) - 1] for j in live)
            batch = []
            for j in live:
                start = pos[j]
                end = bisect_right(runs[j], pivot, start)
                if end > start:
                    batch += runs[j][start:end]
                    if release:
                        runs[j][start:end] = [None] * (end - start)
                    pos[j] = end
            live = [j for j in live if pos[j] < lens[j]]
            batch.sort()
            yield batch
            batch = []
        for j in live:
            run = runs[j]
            for start in range(pos[j], lens[j], size):
                check(cancel)
                piece = run[start:start + size]
                if release:
                    run[start:start + len(piece)] = [None] * len(piece)
                yield piece
    except BaseException:
        _release(batch, *runs)
        raise


def _sorted_pieces(entries: list[bytes], slice_size: int | None = None,
                   cancel: CancelToken | None = None) -> Iterator[list[bytes]]:
    """Sorted consecutive pieces of ``entries`` (their concatenation equals
    ``sorted(entries)``) without a single long C call. ``list.sort`` holds the GIL for its
    whole run (~0.5 s for 3 million keys), and the GUI thread needs the GIL for every
    Qt-to-Python call -- each of its many hand-overs waits for the worker's current C call,
    so even 20 ms calls add up to visible stalls. The keys are sorted in slices of at most
    ``slice_size`` and the slices merged :data:`SORT_MERGE_FANIN` at a time, level by level
    (:func:`_merge_runs`: every step a few ``bisect`` calls and a short ``list.sort`` of a
    few sorted runs); the last level is not stored but yielded batch by batch. The work is
    close to one ``sort`` (plus copying the key references once per level); the GIL is
    free between the steps. Equal keys are equal bytes, so the result is exactly
    ``sorted(entries)``. ``entries`` is consumed (emptied).

    Cancellation is checked for every slice and every merge batch (a few ms apart: a
    cancelled 10-million-key sort stops at once instead of after this whole sort). When the
    generator ends early -- cancelled, or closed by its consumer -- every list it holds
    (``entries``, the runs, the level being merged) is emptied at once: the key references
    go before the exception (whose traceback keeps the frame) is dropped."""
    size = max(2, int(SORT_SLICE if slice_size is None else slice_size))
    fan_in = max(2, int(SORT_MERGE_FANIN))
    if len(entries) <= size:
        check(cancel)
        entries.sort()
        piece = entries[:]
        entries.clear()
        if piece:
            yield piece
        return
    runs: list[list[bytes]] = []
    merged: list[list[bytes]] = []
    group: list[list[bytes]] = []
    out: list[bytes] = []
    try:
        while entries:               # from the end: no memmove, at most one extra slice in memory
            check(cancel)
            chunk = entries[-size:]
            del entries[-size:]
            chunk.sort()
            runs.append(chunk)
        while len(runs) > fan_in:
            merged = []
            for g in range(0, len(runs), fan_in):
                group = runs[g:g + fan_in]
                for j in range(g, min(g + fan_in, len(runs))):
                    runs[j] = []
                out = []
                for batch in _merge_runs(group, size, cancel=cancel):
                    out += batch
                # the group's lists go one by one (references only: the keys live on in out)
                while group:
                    group.pop()
                merged.append(out)
                out = []
            runs = merged
            merged = []
        yield from _merge_runs(runs, size, release=True, cancel=cancel)
    except BaseException:
        _release(entries, out, *runs, *merged, *group)
        runs.clear()
        merged.clear()
        group.clear()
        raise


def _free_lists(lists: list[list[Any]]) -> None:
    for lst in lists:
        while lst:
            del lst[-RELEASE_SLICE:]   # from the end: no memmove; the GIL is free between the steps


def _release(*lists: list[Any]) -> threading.Thread | None:
    """Empty the key lists of a sort that ended early. Up to :data:`RELEASE_SLICE` items
    in all: at once. More (a cancelled 10-million-key sort holds millions of keys): by a
    short daemon thread, :data:`RELEASE_SLICE` keys per step -- freeing 4 million keys
    takes ~0.2 s, which neither delays the end of the cancelled task nor holds the GIL in
    one piece (the GUI thread gets it between the steps). Returns that thread."""
    todo = [lst for lst in lists if lst]
    if not todo:
        return None
    if sum(map(len, todo)) <= RELEASE_SLICE:
        _free_lists(todo)
        return None
    thread = threading.Thread(target=_free_lists, args=(todo,), name="jsonreshape-release", daemon=True)
    thread.start()
    return thread


def _seqs_of(entries: list[bytes]) -> array:
    """Sequence numbers (the 8-byte big-endian suffix) of composite keys."""
    a = array("Q")
    a.frombytes(b"".join(map(_TAIL, entries)))
    if not _BIG_ENDIAN:
        a.byteswap()
    return a


class _Sorter:
    """Accumulates composite keys; sorts in memory or spills sorted runs to disk.

    ``directory``: where the run directory is created (default ``tmp_dir()``);
    ``max_limit``: the largest run size the limit may grow to; ``expected``: the number of
    keys expected (``None`` = unknown) -- used to keep the number of runs near
    :data:`TARGET_RUNS` (module docstring)."""

    def __init__(self, limit: int, cancel: CancelToken | None, fan_in: int | None = None, *,
                 directory: Path | None = None, max_limit: int | None = None, expected: int | None = None) -> None:
        self.limit = max(1, int(limit))
        self.cancel = cancel
        self.fan_in = max(2, int(MAX_FANIN if fan_in is None else fan_in))
        self.directory = directory
        self.max_limit = max(self.limit, int(max_limit)) if max_limit is not None else self.limit
        self.expected = expected
        self.entries: list[bytes] = []
        self.mem = 0
        self.peak = 0
        self.count = 0
        self.key_bytes = 0
        self.placed = 0          # keys placed in memory or in runs so far (for the run size)
        self.placed_mem = 0      # ... and their estimated bytes
        self.runs: list[Path] = []
        self.runs_written = 0
        self.merge_passes = 0
        self.dir: Path | None = None
        self._files: list[Any] = []

    # -- accumulating ------------------------------------------------------------------
    def add(self, new: list[bytes]) -> None:
        """Add composite keys; spills a sorted run whenever the estimate would exceed the
        limit (a single key larger than the limit becomes a run of its own)."""
        if not new:
            return
        n = len(new)
        size = sum(map(len, new))
        self.count += n
        self.key_bytes += size
        nbytes = size + ENTRY_OVERHEAD * n
        if self.mem + nbytes <= self.limit:
            self.entries.extend(new)
            self.mem += nbytes
            self.placed += n
            self.placed_mem += nbytes
            self.peak = max(self.peak, self.mem)
            return
        # cum[j]: estimated bytes of new[0..j]
        cum = list(itertools.accumulate(map(operator.add, map(len, new), itertools.repeat(ENTRY_OVERHEAD))))
        pos = 0
        base = 0
        while pos < n:
            k = bisect.bisect_right(cum, base + self.limit - self.mem, pos)  # new[pos:k] fits
            if k == pos:
                if self.entries:
                    self._spill()
                    continue
                k = pos + 1
            self.entries.extend(new[pos:k])
            self.mem += cum[k - 1] - base
            self.placed += k - pos
            self.placed_mem += cum[k - 1] - base
            self.peak = max(self.peak, self.mem)
            base = cum[k - 1]
            pos = k

    def _run_path(self) -> Path:
        if self.dir is None:
            base = self.directory if self.directory is not None else tmp_dir()
            for _ in range(100):
                d = base / f"sort-{os.getpid()}-{secrets.token_hex(4)}"
                try:
                    os.mkdir(long_path(d))
                    break
                except FileExistsError:
                    continue
                except OSError as exc:
                    raise os_error(exc, d) from exc
            else:  # pragma: no cover
                raise JsonReshapeError("recordset.error.write", path=str(base), detail="no temporary name")
            self.dir = d
        self.runs_written += 1
        return self.dir / f"run-{self.runs_written:06d}.bin"

    def _write(self, entries: Iterable[bytes]) -> Path:
        p = self._run_path()
        try:
            with open(long_path(p), "wb", buffering=RUN_WRITE_BYTES) as f:
                _write_run(f, entries, self.cancel)
        except OSError as exc:
            raise os_error(exc, p) from exc
        return p

    def _spill(self) -> None:
        check(self.cancel)
        if not self.entries:
            return
        entries, self.entries = self.entries, []
        self.mem = 0
        pieces = _sorted_pieces(entries, cancel=self.cancel)
        try:
            self.runs.append(self._write(itertools.chain.from_iterable(pieces)))
        finally:
            pieces.close()      # cancelled: the generator empties its lists now
            _release(entries)
        self._grow()
        check(self.cancel)

    def _grow(self) -> None:
        """Keep the number of runs near TARGET_RUNS: when the keys seen so far project
        more runs, raise the run size (never above ``max_limit``)."""
        if self.limit >= self.max_limit:
            return
        written = self.runs_written
        expected = self.expected
        placed = self.placed
        if expected is not None and placed > 0:
            if expected <= placed:
                return
            remaining = (expected - placed) * (self.placed_mem / placed)
            projected = written + remaining / self.limit
            if projected <= TARGET_RUNS:
                return
            room = max(TARGET_RUNS // 4, TARGET_RUNS - written)
            new_limit = int(remaining / room) + 1
        elif written % (TARGET_RUNS // 4) == 0:   # unknown size: double now and then
            new_limit = 2 * self.limit
        else:
            return
        new_limit = min(self.max_limit, max(self.limit, new_limit))
        if new_limit > self.limit:
            log.info("sort: %d runs written; run size raised from %s to %s", written, format_bytes(self.limit),
                     format_bytes(new_limit))
            self.limit = new_limit

    # -- output ------------------------------------------------------------------------
    def _open_runs(self, runs: list[Path], bufsize: int) -> list[Iterator[bytes]]:
        iters = []
        for p in runs:
            try:
                f = open(long_path(p), "rb", buffering=0)  # noqa: SIM115 - closed in close_files()
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

    def _bufsize(self, nruns: int) -> int:
        return max(64 * KiB, min(RUN_READ_BYTES, self.limit // (2 * max(1, nruns))))

    def _reduce_runs(self) -> None:
        """Merge runs ``fan_in`` at a time until one final merge suffices (leaves room
        for the in-memory part)."""
        while len(self.runs) + (1 if self.entries else 0) > self.fan_in:
            check(self.cancel)
            group = self.runs[:self.fan_in]
            iters = self._open_runs(group, self._bufsize(len(group)))
            try:
                merged = self._write(heapq.merge(*iters))   # checks cancellation as it writes
            finally:
                self._close_files()
            for p in group:
                _remove_file(p)
            self.runs = self.runs[self.fan_in:] + [merged]
            self.merge_passes += 1

    def sorted_batches(self, size: int = CHUNK_ITEMS) -> Iterator[tuple[int, array]]:
        """``(key bytes, sequence numbers)`` batches in sorted order. Close the generator
        when stopping early (cancellation): every key list it holds is emptied then."""
        if not self.runs:
            entries, self.entries = self.entries, []
            pending: list[bytes] = []
            pieces = _sorted_pieces(entries, cancel=self.cancel)
            try:
                for piece in pieces:
                    pending += piece
                    del piece
                    while len(pending) >= size:
                        chunk = pending[:size]
                        del pending[:size]
                        yield sum(map(len, chunk)), _seqs_of(chunk)
                        del chunk
                if pending:
                    yield sum(map(len, pending)), _seqs_of(pending)
            finally:
                pieces.close()
                _release(entries, pending)
            return
        self._reduce_runs()
        iters = self._open_runs(self.runs, self._bufsize(len(self.runs)))
        mem_pieces = None
        entries = self.entries
        if entries:
            self.entries = []
            mem_pieces = _sorted_pieces(entries, cancel=self.cancel)
            iters.insert(0, itertools.chain.from_iterable(mem_pieces))
        merged = heapq.merge(*iters)
        try:
            while True:
                chunk = list(itertools.islice(merged, size))
                if not chunk:
                    break
                yield sum(map(len, chunk)), _seqs_of(chunk)
                del chunk
        finally:
            if mem_pieces is not None:
                mem_pieces.close()
                _release(entries)
            self._close_files()
        self.entries = []

    def cleanup(self) -> None:
        """Close run files and remove the temporary directory (never raises); the keys
        still held are released (:func:`_release`)."""
        self._close_files()
        entries, self.entries = self.entries, []
        _release(entries)
        d, self.dir = self.dir, None
        self.runs = []
        if d is not None:
            _remove_tree(d)


def _seq_mapper(src: Any) -> Callable[[array], array]:
    """Function mapping sequence numbers (positions in the source) to record indices."""
    if src is None:
        return lambda seqs: seqs
    if isinstance(src, range):
        start, step = src.start, src.step
        if start == 0 and step == 1:
            return lambda seqs: seqs
        return lambda seqs: array("Q", [start + s * step for s in seqs])
    if isinstance(src, RecordSet):
        return src._lookup_many
    getter = src.__getitem__
    return lambda seqs: array("Q", map(getter, seqs))


def _normalize_keys(keys: Any) -> list[SortKey]:
    if isinstance(keys, (SortKey, str)):
        keys = [keys]
    specs: list[SortKey] = []
    for k in keys:
        if isinstance(k, SortKey):
            specs.append(k)
        elif isinstance(k, str):
            specs.append(SortKey.parse(k))
        else:
            raise TypeError(f"sort keys must be SortKey or str, not {type(k).__name__}")
    if not specs:
        raise UserInputError("sortkeys.err.empty_field")
    return specs


def sort_records(doc: Any, keys: Sequence[SortKey | str], source: Any = None, *, memory_limit: int | None = None,
                 progress: Progress | None = None, cancel: CancelToken | None = None,
                 block_bytes: int = BLOCK_BYTES) -> RecordSet:
    """The records of ``source`` sorted by ``keys`` (spec §6, AGENT_PROMPT §6.4).

    Stable (ties keep source order); ``memory_limit`` (bytes, default the document's
    ``Settings.sort_memory_bytes``; at most half of ``Settings.memory_limit_bytes``)
    bounds the in-memory keys -- beyond it sorted runs go to temporary files in
    ``appdirs.work_dir(settings.cache_dir)`` and are merged (external sort; same result).
    ``source``: ``None`` (all records), a RecordSet, a ``range``, a list or any iterable
    of record indices. The result's ``report`` is a :class:`SortReport`. Raises
    :class:`~jsonreshape.errors.Cancelled` (temporary files removed) when cancelled and
    :class:`~jsonreshape.errors.FileChangedError` when the file changed during the sort.
    """
    specs = _normalize_keys(keys)
    settings = getattr(doc, "settings", None) or get_settings()
    own = memory_limit if memory_limit is not None else settings.sort_memory_bytes
    if isinstance(own, bool) or not isinstance(own, int) or own < 1:
        raise UserInputError("recordset.error.memory_limit", value=repr(memory_limit))
    budget = max(1, int(settings.memory_limit_bytes) // 2)   # the overall memory budget's share
    limit = min(own, budget)
    if limit < own:
        log.info("sort: memory limit %s lowered to %s (half of the memory limit setting)", format_bytes(own),
                 format_bytes(limit))
    started = time.perf_counter()
    doc = _pin(doc)
    _check_scope(doc, source)
    src = _normalize_source(doc, source)
    temp_src: RecordSet | None = None
    if src is not None and not isinstance(src, (RecordSet, range, list, tuple, array)):
        temp_src = src = RecordSet.from_indices(src, kind="manual")
    collector = _KeyCollector(doc, specs, cancel)
    expected: int | None
    try:
        expected = len(src) if src is not None else (int(doc.record_count) if doc.index_complete else None)
    except TypeError:
        expected = None
    sorter = _Sorter(limit, cancel, directory=work_dir(settings.cache_dir), max_limit=budget, expected=expected)
    name = doc.source_path.name
    tracker = _Tracker(progress, _estimate_total(doc, src), t("recordset.progress.sort_keys", name=name))
    builder = RecordSetBuilder("sort", t("recordset.desc.sort", keys=describe_keys(specs)), gen=getattr(doc, "gen", None))
    try:
        try:
            seq = 0
            for blk in doc.iter_blocks(src, block_bytes=block_bytes, cancel=cancel):
                if not collector.started:
                    collector.start()
                if blk.huge:
                    entries = [collector.huge(blk, seq)]
                    s, e = blk.span(0)
                    nbytes = e - s
                else:
                    entries = collector.block(blk, seq)
                    nbytes = len(blk.data)
                seq += len(entries)
                sorter.add(entries)
                del entries      # the sorter holds the keys now (this list would outlive the reading)
                tracker.add_keys(nbytes, sorter.key_bytes)
                check(cancel)
            _ensure_unchanged(doc)
            bytes_read = tracker.done
            runs = sorter.runs_written
            tracker.phase(sorter.key_bytes, t("recordset.progress.sort_merge", name=name))
            mapper = _seq_mapper(src)
            batches = sorter.sorted_batches()
            try:
                for nkey, seqs in batches:
                    builder.extend(mapper(seqs))
                    tracker.add(nkey)
                    check(cancel)
            finally:
                batches.close()   # cancelled: the merge's key lists go now, not with the traceback
            rs = builder.finish()
        finally:
            sorter.cleanup()
            if temp_src is not None:
                temp_src.close()
            # the encoders' memoised keys were made among the sort's millions of keys: each
            # one left alive pins a whole 1 MiB pymalloc arena of freed keys (a cancelled
            # 10M sort kept ~120 MB private memory that way; cleared: back to the start)
            clear_caches()
    except BaseException:
        builder.abort()
        raise
    tracker.finish()
    warnings: list[str] = []
    params: list[dict[str, Any]] = []
    if collector.approximate:
        warnings.append("recordset.warn.approximate")
        params.append(dict(_huge_warning_params(doc, collector.approximate), record=collector.first_approximate))
        log.warning("sort of %s: keys of %d huge records were built from a prefix of the value", name,
                    collector.approximate)
    total = sorter.count
    for spec, missing in zip(specs, collector.missing_counts):
        if spec.kind == "index" or total == 0:
            continue
        if missing == total:
            warnings.append("recordset.warn.sort_key_absent")
            params.append({"field": spec.path, "total": total})
            log.warning("sort of %s: the field %s is missing in all %d records", name, spec.path, total)
        elif missing * 100 > RARE_KEY_PERCENT * total:
            warnings.append("recordset.warn.sort_key_rare")
            params.append({"field": spec.path, "found": total - missing, "total": total})
    elapsed = time.perf_counter() - started
    rs.report = SortReport(records=sorter.count, runs=runs, elapsed=elapsed, peak_bytes_estimate=sorter.peak,
                           strategy="external" if runs else "memory", bytes_read=bytes_read,
                           key_bytes=sorter.key_bytes, merge_passes=sorter.merge_passes,
                           approximate=collector.approximate, warnings=tuple(warnings), warning_params=tuple(params),
                           missing=tuple(collector.missing_counts), memory_limit=sorter.limit)
    log.info("sorted %d records of %s by %s in %.2f s (%s, %d runs, peak ~%s)", sorter.count, name,
             ", ".join(s.to_text() for s in specs), elapsed, rs.report.strategy, runs, format_bytes(sorter.peak))
    return rs
