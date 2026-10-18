"""On-disk binary span index (docs/ARCHITECTURE.md §5.2; AGENT_PROMPT §3 "Indeks").

An index file stores ``count`` record spans ``(start, end)`` and a JSON metadata object.
It lives in ``appdirs.index_dir(settings.cache_dir)`` as ``<key>.jri`` (see
:func:`cache_key`) and is read through a read-only memory map: spans are never loaded
into RAM (``memoryview.cast("Q")`` over the mapping).

File layout (all integers little-endian)::

    offset        size       field
    0             8          magic  b"JRIDX\\x00\\x01\\x00"
    8             4          version      u32 = INDEX_VERSION (1)
    12            4          flags        u32 (see below)
    16            8          count        u64 number of spans
    24            8          meta_offset  u64 = HEADER_SIZE + 16 * count
    32            8          meta_length  u64
    40            4056       zeros (reserved)
    4096          16*count   spans: (start, end) u64 pairs
    meta_offset   meta_length  UTF-8 JSON object (metadata)

Flags (``u32``):

* bit 0 :data:`FLAG_COMPLETE` -- the file was finalized (header, spans and metadata all
  written and fsynced). Files without it are never used.
* bit 1 :data:`FLAG_ERROR` -- scanning stopped at a JSON error; the spans are the records
  before the error and ``meta["error"]`` holds the error (``JsonSyntaxError.to_dict()``).
* bit 2 :data:`FLAG_MEMBERS` -- the spans are object member spans (``"key": value``).
* bit 3 :data:`FLAG_BROKEN_LINES` -- JSON Lines broken lines are listed in the metadata.
* bit 4 :data:`FLAG_NODE` -- a node child index (children of one container) rather than a
  document record index.
* bits 5..31 are reserved (written as 0, ignored when read).

Writing (:class:`IndexWriter`) goes to a temporary file next to the final name; spans
are buffered in memory (at most :data:`FLUSH_BYTES`, ~1 MiB) and readable *while* the
index grows (thread-safe live reads). :meth:`IndexWriter.finalize` writes the metadata
and the header, fsyncs and renames the file to its final name. On Windows a file that is
memory-mapped (by another :class:`IndexReader`) cannot be replaced: when the caller
accepts the existing index (``accept_existing``: same key = same file version, so the
spans are the same) the new one is discarded and the existing one is used; otherwise
the new index is kept under a unique name ``<key>~<8 hex>.jri`` (logged;
``IndexWriter.used_path`` / ``IndexReader.path`` name it and :func:`find_index` finds such
alternates). Installing a canonical index removes the alternates of its key (those still
mapped by someone stay until a later attempt).

Several processes, one key (AGENT_PROMPT §1.7): a finalized file is never deleted by a
writer -- another process may have installed it under the same name a moment earlier --
and opening the installed file is retried briefly (a concurrent rename or a scanner can
hold it for a moment); when it still cannot be opened, any complete index of the key is
used. :class:`BuildLock` serializes the builds of one key across processes (a lock file
``<key>.lock`` next to the index, locked with ``msvcrt.locking`` / ``fcntl.flock``, so the
lock of a killed process is released by the OS): the reader takes it before indexing, a
second process waits and then reuses the index the first one installed.

:func:`evict` deletes least-recently-used index files (the modification time is touched
every time a file is opened) -- and, with ``prepared_dir``, the prepared copies of
:mod:`jsonreshape.fileio` in the same budget -- until the removable files fit
``Settings.cache_limit_bytes``; unused alternates go first. Files that are kept anyway
(in use by this or another process, ``keep``, prepared copies used within the last hour)
are not counted against the limit (:func:`jsonreshape.fileio.evict_entries`): a few big,
recently used prepared copies must not make every index file go.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mmap
import os
import secrets
import struct
import sys
import threading
import time
from array import array
from pathlib import Path
from typing import Any, Callable, Iterable, Union

from .appdirs import index_dir
from .errors import IndexBuildError
from .fileio import (
    CacheEntry,
    FileIdentity,
    evict_entries,
    long_path,
    make_dirs,
    normalize_path,
    os_error,
    prepared_cache_entries,
)

log = logging.getLogger(__name__)

StrPath = Union[str, "os.PathLike[str]"]

__all__ = [
    "FLAG_BROKEN_LINES",
    "FLAG_COMPLETE",
    "FLAG_ERROR",
    "FLAG_MEMBERS",
    "FLAG_NODE",
    "FLUSH_BYTES",
    "HEADER_SIZE",
    "INDEX_SUFFIX",
    "INDEX_VERSION",
    "MAGIC",
    "BuildLock",
    "IndexReader",
    "IndexWriter",
    "cache_key",
    "default_index_dir",
    "evict",
    "find_index",
    "index_path",
    "lock_path",
]

MAGIC = b"JRIDX\x00\x01\x00"
INDEX_VERSION = 1
HEADER_SIZE = 4096
INDEX_SUFFIX = ".jri"
LOCK_SUFFIX = ".lock"
FLUSH_BYTES = 1 << 20             # unflushed span bytes kept in memory by IndexWriter
TEMP_SUFFIX = ".tmp"
TEMP_MAX_AGE = 86400.0            # leftover temp files older than this are removed by evict()
MAX_META_BYTES = 1 << 30          # sanity limit when reading metadata
OPEN_RETRY_SECONDS = 2.0          # finalize(): how long opening the installed index is retried

FLAG_COMPLETE = 1 << 0
FLAG_ERROR = 1 << 1
FLAG_MEMBERS = 1 << 2
FLAG_BROKEN_LINES = 1 << 3
FLAG_NODE = 1 << 4
_KNOWN_FLAGS = FLAG_COMPLETE | FLAG_ERROR | FLAG_MEMBERS | FLAG_BROKEN_LINES | FLAG_NODE

# magic(8s) version(I) flags(I) count(Q) meta_offset(Q) meta_length(Q)
_HEADER = struct.Struct("<8sIIQQQ")
_PAIR = struct.Struct("<QQ")
_BIG_ENDIAN = sys.byteorder == "big"
_WINDOWS = sys.platform == "win32"


# ----------------------------------------------------------------------------------
# keys and locations
# ----------------------------------------------------------------------------------
def cache_key(identity: FileIdentity, kind: str, record_path: Iterable[str | int] | None,
              extra: str = "") -> str:
    """Hex sha256 naming the index of one file version, format and record path.

    Hashes the identity (normalized path, size, mtime_ns, head hash, tail hash, sample
    hash), the format ``kind``, the record path, :data:`INDEX_VERSION` and ``extra`` (node
    child indexes use ``"node:<start offset>"``). Any change of the file that the identity
    sees gives a new key (an edit that keeps size and mtime and misses the hashed ranges
    does not: see :class:`~jsonreshape.fileio.FileIdentity`).
    """
    rp = None if record_path is None else [seg if isinstance(seg, int) else str(seg) for seg in record_path]
    payload = json.dumps(
        [identity.path, identity.size, identity.mtime_ns, identity.head_hash, identity.tail_hash,
         identity.sample_hash, kind, rp, INDEX_VERSION, extra],
        ensure_ascii=True,  # lone surrogates in keys stay encodable
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def default_index_dir(cache_dir: StrPath | None = None) -> Path:
    """``appdirs.index_dir(cache_dir)`` (``cache_dir`` = ``Settings.cache_dir``)."""
    return index_dir(cache_dir)


def index_path(key: str, directory: StrPath | None = None) -> Path:
    """Canonical file name of the index ``key``."""
    base = Path(directory) if directory is not None else default_index_dir()
    return base / f"{key}{INDEX_SUFFIX}"


def lock_path(key: str, directory: StrPath | None = None) -> Path:
    """File name of the build lock of the index ``key`` (see :class:`BuildLock`)."""
    base = Path(directory) if directory is not None else default_index_dir()
    return base / f"{key}{LOCK_SUFFIX}"


def _alternates(final: Path) -> list[Path]:
    """Uniquely named fallbacks ``<stem>~<hex>.jri`` of ``final``, newest first."""
    prefix = final.stem + "~"
    try:
        names = os.listdir(long_path(final.parent))
    except OSError:
        return []
    found = []
    for name in names:
        if name.startswith(prefix) and name.endswith(INDEX_SUFFIX):
            p = final.parent / name
            try:
                found.append((os.stat(long_path(p)).st_mtime_ns, p))
            except OSError:
                continue
    found.sort(key=lambda e: e[0], reverse=True)
    return [p for _, p in found]


def find_index(key: str, directory: StrPath | None = None) -> IndexReader | None:
    """Open the complete index ``key`` (canonical name first, then unique-name
    alternates written while the canonical file was in use); ``None`` if there is none."""
    return _find_at(index_path(key, directory))


def _find_at(final: Path, *, touch: bool = True) -> IndexReader | None:
    reader = IndexReader.open(final, touch=touch)
    if reader is not None:
        return reader
    for alt in _alternates(final):
        reader = IndexReader.open(alt, touch=touch)
        if reader is not None:
            return reader
    return None


def _remove_alternates(final: Path) -> None:
    """Delete the unique-name alternates of ``final`` (best effort: one still mapped by
    a reader stays)."""
    for alt in _alternates(final):
        try:
            os.remove(long_path(alt))
            log.info("removed the duplicate index %s", alt.name)
        except OSError:
            log.debug("duplicate index %s is in use; kept", alt.name)


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def _pairs_from_bytes(data: bytes) -> array:
    a = array("Q")
    a.frombytes(data)
    if _BIG_ENDIAN:  # pragma: no cover - little-endian hosts only
        a.byteswap()
    return a


def _check_spans(spans: bytes | bytearray | memoryview) -> bytes:
    if isinstance(spans, str):
        raise TypeError("span arrays are bytes, not str")
    b = bytes(spans)
    if len(b) % 16:
        raise ValueError(f"span array length {len(b)} is not a multiple of 16")
    return b


def _remove_quiet(p: Path) -> None:
    try:
        os.remove(long_path(p))
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("could not remove %s", p, exc_info=True)


def _touch(p: Path) -> None:
    """Mark ``p`` as recently used (LRU); fails silently when the file is in use."""
    try:
        os.utime(long_path(p))
    except OSError:
        pass


# ----------------------------------------------------------------------------------
# reader
# ----------------------------------------------------------------------------------
class IndexReader:
    """A finalized index file, memory-mapped read-only.

    ``count`` spans are served from the mapping (never read into RAM); ``meta`` is the
    parsed metadata object. Use :meth:`open` (returns ``None`` for missing, corrupt,
    incomplete or wrong-version files instead of raising). Thread-safe for reading;
    :meth:`close` is idempotent and must not race with reads.
    """

    def __init__(self, path: Path, mm: mmap.mmap, version: int, flags: int, count: int,
                 meta: dict[str, Any], file_size: int) -> None:
        self.path = path
        self.version = version
        self.flags = flags
        self.count = count
        self.meta = meta
        self.file_size = file_size
        self._mm: mmap.mmap | None = mm
        self._view: memoryview | None = memoryview(mm)
        span_view = self._view[HEADER_SIZE:HEADER_SIZE + 16 * count]
        self._spans: memoryview | None = span_view.cast("Q")
        span_view.release()
        self._lock = threading.Lock()
        self._rf: Any = None  # lazily opened handle for read_many()

    @classmethod
    def open(cls, path: StrPath, *, touch: bool = True) -> IndexReader | None:
        """Open and validate ``path``; ``None`` when it cannot be used (never raises
        for bad files). ``touch=True`` updates the modification time (LRU) first."""
        p = Path(path)
        if touch:
            _touch(p)  # before mapping: os.utime needs exclusive access on Windows
        try:
            f = open(long_path(p), "rb")  # noqa: SIM115 - plain open: in-use files resist eviction
        except OSError:
            return None
        mm = None
        try:
            size = os.fstat(f.fileno()).st_size
            if size < HEADER_SIZE:
                return None
            header = f.read(_HEADER.size)
            if len(header) != _HEADER.size:
                return None
            magic, version, flags, count, meta_offset, meta_length = _HEADER.unpack(header)
            if magic != MAGIC or version != INDEX_VERSION or not flags & FLAG_COMPLETE:
                return None
            if meta_offset != HEADER_SIZE + 16 * count or meta_length > MAX_META_BYTES:
                return None
            if meta_offset + meta_length > size:
                return None
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                meta = json.loads(mm[meta_offset:meta_offset + meta_length].decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                mm.close()
                return None
            if not isinstance(meta, dict) or meta.get("index_version") != INDEX_VERSION:
                mm.close()
                return None
            reader = cls(p, mm, version, flags, count, meta, size)
            mm = None
            return reader
        except (OSError, ValueError, struct.error):
            log.debug("cannot open index %s", p, exc_info=True)
            if mm is not None:
                mm.close()
            return None
        finally:
            f.close()  # the mapping keeps its own handle

    # -- access ------------------------------------------------------------------------
    @property
    def closed(self) -> bool:
        return self._spans is None

    @property
    def complete(self) -> bool:
        return bool(self.flags & FLAG_COMPLETE)

    def _arr(self) -> memoryview:
        spans = self._spans
        if spans is None:
            raise ValueError("index reader is closed")
        return spans

    def get(self, i: int) -> tuple[int, int]:
        """Span ``i`` as ``(start, end)`` (``IndexError`` when out of range)."""
        if i < 0 or i >= self.count:
            raise IndexError(f"span index {i} out of range (count {self.count})")
        spans = self._arr()
        return spans[2 * i], spans[2 * i + 1]

    def get_many(self, i: int, n: int) -> bytes:
        """Spans ``i .. i+n-1`` as u64 LE pairs (clipped to ``count``)."""
        if i < 0 or n < 0:
            raise IndexError(f"invalid span range {i}+{n}")
        stop = min(self.count, i + n)
        if i >= stop:
            return b""
        spans = self._arr()
        return spans[2 * i:2 * stop].tobytes()

    def read_many(self, i: int, n: int) -> bytes:
        """Like :meth:`get_many`, but through an ordinary file read instead of the
        mapping. For sequential scans of big indexes: pages touched through the mapping
        stay in this process's working set (RSS) until the OS trims them, while file
        reads only go through the system cache."""
        if i < 0 or n < 0:
            raise IndexError(f"invalid span range {i}+{n}")
        stop = min(self.count, i + n)
        if i >= stop:
            return b""
        want = 16 * (stop - i)
        with self._lock:
            if self._spans is None:
                raise ValueError("index reader is closed")
            f = self._rf
            if f is None:
                f = self._rf = open(long_path(self.path), "rb", buffering=0)  # noqa: SIM115
            f.seek(HEADER_SIZE + 16 * i)
            data = f.read(want)
            while len(data) < want:
                more = f.read(want - len(data))
                if not more:  # pragma: no cover - a finalized index has all its spans
                    raise ValueError(f"index {self.path} is truncated")
                data += more
        return data

    def spans_array(self, i: int = 0, n: int | None = None) -> array:
        """Spans ``i .. i+n-1`` as an ``array('Q')`` copy (``n=None``: to the end)."""
        if n is None:
            n = self.count - i
        return _pairs_from_bytes(self.get_many(i, n))

    def read_meta(self) -> dict[str, Any]:
        """Parse the metadata again from the mapping (callers may drop large parts of
        :attr:`meta` and re-read them later)."""
        mm = self._mm
        if mm is None:
            raise ValueError("index reader is closed")
        meta_offset = HEADER_SIZE + 16 * self.count
        header = _HEADER.unpack(mm[:_HEADER.size])
        return json.loads(mm[meta_offset:meta_offset + header[5]].decode("utf-8"))

    def close(self) -> None:
        """Release the mapping (memoryviews first); idempotent."""
        with self._lock:
            spans, view, mm = self._spans, self._view, self._mm
            self._spans = self._view = self._mm = None
            rf, self._rf = self._rf, None
        if rf is not None:
            rf.close()
        if spans is not None:
            spans.release()
        if view is not None:
            view.release()
        if mm is not None:
            try:
                mm.close()
            except BufferError:  # pragma: no cover - a caller kept an exported view
                log.warning("index %s still has exported views; mapping left open", self.path)

    def __enter__(self) -> IndexReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __len__(self) -> int:
        return self.count

    def __repr__(self) -> str:
        state = "closed" if self.closed else f"{self.count} spans"
        return f"<IndexReader {self.path.name} {state} flags={self.flags:#x}>"


# ----------------------------------------------------------------------------------
# writer
# ----------------------------------------------------------------------------------
class IndexWriter:
    """Builds an index file; spans are readable while it grows.

    ``append()`` takes span arrays exactly as returned by ``core.Scanner.feed()``. At
    most ~``flush_bytes`` are kept in memory; older spans are on disk in the temporary
    file ``path`` (next to ``final_path``). ``count``, :meth:`get` and :meth:`get_many`
    are thread-safe and may be called from other threads during writing; after
    :meth:`finalize` they are served by the returned :class:`IndexReader` (which the
    caller owns and closes). :meth:`abort` removes the temporary file.

    OS errors are raised as localized errors (``fileio.error.disk_full`` or
    :class:`~jsonreshape.errors.IndexBuildError` ``index.error.write``).
    """

    def __init__(self, final_path: StrPath, *, flush_bytes: int = FLUSH_BYTES) -> None:
        self.final_path = Path(os.path.abspath(os.fspath(final_path)))
        self.used_path: Path | None = None       # set by finalize()
        self.unique_fallback = False             # final name was in use (Windows)
        self._flush_bytes = max(16, int(flush_bytes))
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._flushed = 0                        # spans already in the file
        self._count = 0
        self._reader: IndexReader | None = None
        self._state = "open"                     # open | final | aborted
        self.reused_existing = False             # finalize() kept an equivalent installed index
        try:
            make_dirs(self.final_path.parent)
        except OSError as exc:
            raise self._os_error(exc, self.final_path.parent) from exc
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        for _ in range(100):
            tmp = self.final_path.with_name(
                f".{self.final_path.stem[:40]}.{os.getpid()}.{secrets.token_hex(4)}{TEMP_SUFFIX}")
            try:
                fd = os.open(long_path(tmp), flags, 0o666)
                break
            except FileExistsError:
                continue
            except OSError as exc:
                raise self._os_error(exc, tmp) from exc
        else:  # pragma: no cover
            raise IndexBuildError("index.error.write", path=str(self.final_path), detail="no temporary name")
        self.path = tmp
        try:
            self._wf = os.fdopen(fd, "r+b", buffering=0)
            self._wf.write(bytes(HEADER_SIZE))   # placeholder header (complete flag clear)
            self._rf = open(long_path(tmp), "rb", buffering=0)  # noqa: SIM115 - live reads
        except OSError as exc:
            self._close_files()
            _remove_quiet(tmp)
            raise self._os_error(exc, tmp) from exc

    # -- errors --------------------------------------------------------------------------
    @staticmethod
    def _os_error(exc: OSError, path: Path) -> Exception:
        err = os_error(exc, path)
        if err.key == "fileio.error.open":
            return IndexBuildError("index.error.write", path=str(path), detail=exc.strerror or str(exc))
        return err

    # -- writing -------------------------------------------------------------------------
    def append(self, spans: bytes | bytearray | memoryview) -> None:
        """Append u64 LE ``(start, end)`` pairs."""
        data = _check_spans(spans)
        if not data:
            return
        with self._lock:
            if self._state != "open":
                raise ValueError(f"index writer is {self._state}")
            if len(data) >= self._flush_bytes:
                # a big batch goes straight to the file (no second in-memory copy)
                self._flush_locked()
                self._write_locked(data)
                self._flushed += len(data) // 16
                self._count += len(data) // 16
                return
            self._buf += data
            self._count += len(data) // 16
            if len(self._buf) >= self._flush_bytes:
                self._flush_locked()

    def _write_locked(self, data: bytes | bytearray) -> None:
        """Write ``data`` at the end of the flushed spans."""
        try:
            self._wf.seek(HEADER_SIZE + 16 * self._flushed)
            with memoryview(data) as view:
                done = 0
                while done < len(view):
                    done += self._wf.write(view[done:])
        except OSError as exc:
            raise self._os_error(exc, self.path) from exc

    def _flush_locked(self) -> None:
        if not self._buf:
            return
        self._write_locked(self._buf)
        self._flushed += len(self._buf) // 16
        self._buf = bytearray()

    # -- live reads ------------------------------------------------------------------
    @property
    def count(self) -> int:
        reader = self._reader
        if reader is not None:
            return reader.count
        return self._count

    def get(self, i: int) -> tuple[int, int]:
        """Span ``i`` (``IndexError`` when not written yet)."""
        reader = self._reader
        if reader is not None:
            return reader.get(i)
        with self._lock:
            reader = self._reader
            if reader is not None:
                return reader.get(i)
            if i < 0 or i >= self._count:
                raise IndexError(f"span index {i} out of range (count {self._count})")
            if i >= self._flushed:
                return _PAIR.unpack_from(self._buf, 16 * (i - self._flushed))
            return _PAIR.unpack(self._read_file(i, 1))

    def get_many(self, i: int, n: int) -> bytes:
        """Spans ``i .. i+n-1`` as u64 LE pairs (clipped to the current count)."""
        reader = self._reader
        if reader is not None:
            return reader.get_many(i, n)
        if i < 0 or n < 0:
            raise IndexError(f"invalid span range {i}+{n}")
        with self._lock:
            reader = self._reader
            if reader is not None:
                return reader.get_many(i, n)
            stop = min(self._count, i + n)
            if i >= stop:
                return b""
            parts = []
            if i < self._flushed:
                k = min(stop, self._flushed) - i
                parts.append(self._read_file(i, k))
            if stop > self._flushed:
                lo = max(i, self._flushed) - self._flushed
                parts.append(bytes(self._buf[16 * lo:16 * (stop - self._flushed)]))
            return b"".join(parts)

    def _read_file(self, i: int, n: int) -> bytes:
        want = 16 * n
        try:
            self._rf.seek(HEADER_SIZE + 16 * i)
            data = self._rf.read(want)
            while data is not None and len(data) < want:
                more = self._rf.read(want - len(data))
                if not more:
                    break
                data += more
        except OSError as exc:
            raise self._os_error(exc, self.path) from exc
        if data is None or len(data) != want:  # pragma: no cover - flushed data is always there
            raise IndexBuildError("index.error.write", path=str(self.path), detail="short read")
        return data

    # -- completion ------------------------------------------------------------------
    def finalize(self, meta: dict[str, Any], flags: int = 0, *,
                 accept_existing: Callable[[IndexReader], bool] | None = None) -> IndexReader:
        """Write metadata and header, fsync, rename to the final name and return an
        :class:`IndexReader` for it (owned by the caller). ``meta`` must be
        JSON-serializable; ``index_version`` and ``count`` are added. ``flags`` are
        stored with :data:`FLAG_COMPLETE` set.

        ``accept_existing``: when the final name holds a complete index that cannot be
        replaced (Windows: mapped by another reader) and this callback accepts it, the new
        file is discarded and a reader of the existing index is returned
        (``reused_existing``) instead of keeping a duplicate under a unique name.

        Never deletes the installed file: when it cannot be opened (another process
        replaced or evicted it at that moment), opening is retried for
        :data:`OPEN_RETRY_SECONDS`, then any complete index under the final name or its
        alternates is used; only when there is none ``IndexBuildError`` is raised."""
        with self._lock:
            if self._state != "open":
                raise ValueError(f"index writer is {self._state}")
            try:
                self._flush_locked()
                count = self._flushed
                meta = dict(meta)
                meta["index_version"] = INDEX_VERSION
                meta["count"] = count
                # ASCII JSON: lone surrogates (possible in decoded keys) stay representable
                meta_bytes = json.dumps(meta, ensure_ascii=True, separators=(",", ":"),
                                        allow_nan=False).encode("ascii")
                meta_offset = HEADER_SIZE + 16 * count
                self._wf.seek(meta_offset)
                self._wf.write(meta_bytes)
                self._wf.truncate(meta_offset + len(meta_bytes))
                header = _HEADER.pack(MAGIC, INDEX_VERSION, (flags & _KNOWN_FLAGS) | FLAG_COMPLETE, count,
                                      meta_offset, len(meta_bytes))
                self._wf.seek(0)
                self._wf.write(header)
                self._wf.flush()
                os.fsync(self._wf.fileno())
            except OSError as exc:
                self._abort_locked()
                raise self._os_error(exc, self.path) from exc
            except BaseException:
                self._abort_locked()
                raise
            self._close_files()
            used, reader = self._install(accept_existing)
            if reader is None:
                reader = self._open_installed(used)
            if reader is None:
                # the file was replaced or removed by another process right after the
                # rename: never delete anything here, use whatever complete index exists
                reader = _find_at(self.final_path, touch=False)
            if reader is None:
                self._state = "aborted"
                self._buf = bytearray()
                raise IndexBuildError("index.error.write", path=str(used), detail="the installed index vanished")
            if used == self.final_path and not self.reused_existing:
                _remove_alternates(self.final_path)
            self.used_path = reader.path
            self._reader = reader
            self._state = "final"
            self._buf = bytearray()
            return reader

    @staticmethod
    def _open_installed(path: Path) -> IndexReader | None:
        """``IndexReader.open`` retried briefly (a concurrent ``os.replace`` onto the same
        name, or a scanner, can hold the file for a moment)."""
        deadline = time.monotonic() + OPEN_RETRY_SECONDS
        delay = 0.005
        while True:
            reader = IndexReader.open(path, touch=False)
            if reader is not None or time.monotonic() >= deadline:
                return reader
            time.sleep(delay)
            delay = min(delay * 2, 0.1)

    def _install(self, accept_existing: Callable[[IndexReader], bool] | None) -> tuple[Path, IndexReader | None]:
        """Rename the temp file to the final name. When the final name is in use: reuse
        the existing index if ``accept_existing`` agrees (returns its reader), else keep
        the new file under a unique alternate name."""
        deadline = time.monotonic() + 0.25
        delay = 0.01
        while True:
            try:
                os.replace(long_path(self.path), long_path(self.final_path))
                return self.final_path, None
            except PermissionError:
                if not _WINDOWS:
                    self._abort_locked()
                    raise
                if time.monotonic() >= deadline:
                    break
                time.sleep(delay)
                delay = min(delay * 2, 0.08)
            except OSError as exc:
                self._abort_locked()
                raise self._os_error(exc, self.final_path) from exc
        # Windows: the final file is memory-mapped (or locked) by another reader
        if accept_existing is not None:
            existing = IndexReader.open(self.final_path, touch=False)
            if existing is not None:
                try:
                    ok = bool(accept_existing(existing))
                except Exception:  # a broken check must not lose the new index
                    log.warning("checking the existing index %s failed", self.final_path.name, exc_info=True)
                    ok = False
                if ok:
                    _remove_quiet(self.path)
                    self.reused_existing = True
                    log.info("index %s is in use and equivalent; the new copy was discarded", self.final_path.name)
                    return self.final_path, existing
                existing.close()
        for _ in range(100):
            alt = self.final_path.with_name(f"{self.final_path.stem}~{secrets.token_hex(4)}{INDEX_SUFFIX}")
            if os.path.exists(long_path(alt)):
                continue
            try:
                os.replace(long_path(self.path), long_path(alt))
            except OSError as exc:
                self._abort_locked()
                raise self._os_error(exc, alt) from exc
            self.unique_fallback = True
            log.info("index %s is in use; new index kept as %s", self.final_path.name, alt.name)
            return alt, None
        self._abort_locked()  # pragma: no cover
        raise IndexBuildError("index.error.write", path=str(self.final_path), detail="no unique name")

    def abort(self) -> None:
        """Discard the index (temporary file removed); idempotent. A finalized index is
        left alone."""
        with self._lock:
            if self._state == "open":
                self._abort_locked()

    def _abort_locked(self) -> None:
        self._close_files()
        _remove_quiet(self.path)
        self._buf = bytearray()
        self._count = self._flushed = 0
        self._state = "aborted"

    def _close_files(self) -> None:
        for name in ("_wf", "_rf"):
            f = getattr(self, name, None)
            if f is not None:
                try:
                    f.close()
                except OSError:  # pragma: no cover
                    pass
                setattr(self, name, None)

    @property
    def state(self) -> str:
        """``open``, ``final`` or ``aborted``."""
        return self._state

    def __enter__(self) -> IndexWriter:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._state == "open":
            self.abort()

    def __repr__(self) -> str:
        return f"<IndexWriter {self.final_path.name} {self._state} count={self.count}>"


# ----------------------------------------------------------------------------------
# eviction
# ----------------------------------------------------------------------------------
def _index_entries(base: Path, keep_norm: set[str], now: float) -> list[CacheEntry]:
    """The index files of ``base`` as cache entries; leftover temporary files older than
    a day are removed on the way. An alternate (``<key>~<hex>.jri``) whose canonical index
    exists is never read again: it counts as least recently used."""
    try:
        names = os.listdir(long_path(base))
    except OSError:
        return []
    name_set = set(names)
    entries: list[CacheEntry] = []
    for name in names:
        p = base / name
        if name.endswith(TEMP_SUFFIX) and name.startswith("."):
            try:
                if now - os.stat(long_path(p)).st_mtime > TEMP_MAX_AGE:
                    _remove_quiet(p)
            except OSError:
                pass
            continue
        if not name.endswith(INDEX_SUFFIX):
            continue
        try:
            st = os.stat(long_path(p))
        except OSError:
            continue
        last_used = st.st_mtime
        stem = name[:-len(INDEX_SUFFIX)]
        if "~" in stem and stem.split("~", 1)[0] + INDEX_SUFFIX in name_set:
            last_used = 0.0   # a duplicate of an existing canonical index: evicted first
        entries.append(CacheEntry(last_used, st.st_size, (p,), normalize_path(p) in keep_norm))
    return entries


def evict(limit_bytes: int, keep: Iterable[StrPath] = (), *, directory: StrPath | None = None,
          prepared_dir: StrPath | None = None) -> int:
    """Delete least-recently-used cache files until the removable ones hold at most
    ``limit_bytes``; returns the number of bytes freed.

    The cache is the ``*.jri`` files of ``directory`` (default: the app index folder)
    and, with ``prepared_dir``, the prepared (gunzipped / transcoded) copies there
    (:func:`jsonreshape.fileio.prepared_cache_entries`): one budget for both
    (``Settings.cache_limit_bytes``). "Used" = modification time (an index is touched by
    every :meth:`IndexReader.open`, a prepared copy's ``.meta`` whenever it is reused).
    Files in ``keep``, prepared copies open documents of this process read or used within
    the last hour, and files that cannot be deleted because they are in use (Windows
    ``PermissionError`` on a memory-mapped file) are kept *and not counted*
    (:func:`jsonreshape.fileio.evict_entries`): the cache may exceed the limit by their
    size, but they never cause unrelated files to be evicted (two recently used multi-GB
    prepared copies used to wipe the whole index cache for an hour). Leftover temporary
    files older than a day are removed as well.
    """
    base = Path(directory) if directory is not None else default_index_dir()
    keep_list = list(keep)
    keep_norm = {normalize_path(k) for k in keep_list}
    now = time.time()
    entries = _index_entries(base, keep_norm, now)
    if prepared_dir is not None:
        entries += prepared_cache_entries(prepared_dir, keep=keep_list)
    return evict_entries(entries, limit_bytes)


# ----------------------------------------------------------------------------------
# build lock (one builder per key across processes)
# ----------------------------------------------------------------------------------
if _WINDOWS:
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover - closing the handle releases it anyway
            pass
else:  # pragma: no cover - POSIX
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


class BuildLock:
    """Inter-process lock of one index key: the file ``<key>.lock`` next to the index,
    locked with ``msvcrt.locking`` (Windows) / ``fcntl.flock`` (POSIX). The OS drops the
    lock when the holder's handle closes -- also when the process is killed -- so a lock
    is never stale; the file itself is removed on release when nobody else has it open.

    :meth:`acquire` tries once and returns ``False`` when another holder has the lock
    (also a second :class:`BuildLock` of the same key in this process); the caller polls.
    When the lock file cannot be created at all (e.g. a read-only cache folder) the lock
    is a no-op that always succeeds: it is an optimization (no duplicate builds), not a
    correctness requirement.
    """

    DENIED_TRIES = 20   # Windows: a lock file being deleted refuses opens for a moment

    def __init__(self, path: StrPath) -> None:
        self.path = Path(path)
        self._fd: int | None = None
        self.held = False
        self._denied = 0

    def acquire(self) -> bool:
        """Try once to take the lock (``True`` when this object holds it now)."""
        if self.held:
            return True
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0) \
            | getattr(os, "O_CLOEXEC", 0)
        for _ in range(5):
            try:
                fd = os.open(long_path(self.path), flags, 0o666)
            except PermissionError:
                # the previous holder may be deleting it right now ("delete pending"): try
                # again on the next call; a folder that stays unwritable gets no lock
                self._denied += 1
                if self._denied < self.DENIED_TRIES:
                    return False
                log.debug("cannot open the lock file %s; building without a lock", self.path, exc_info=True)
                self.held = True
                return True
            except OSError:
                log.debug("cannot create the lock file %s; building without a lock", self.path, exc_info=True)
                self.held = True
                return True
            if not _try_lock(fd):
                os.close(fd)
                return False
            if not _WINDOWS:  # pragma: no cover - POSIX: the file may have been unlinked meanwhile
                try:
                    same = os.path.samestat(os.fstat(fd), os.stat(long_path(self.path)))
                except OSError:
                    same = False
                if not same:
                    _unlock(fd)
                    os.close(fd)
                    continue
            self._fd = fd
            self.held = True
            return True
        return False  # pragma: no cover

    def release(self) -> None:
        """Release the lock and remove the lock file when nobody else has it open."""
        if not self.held:
            return
        self.held = False
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if not _WINDOWS:  # pragma: no cover - POSIX: unlink while holding the lock
            try:
                os.remove(long_path(self.path))
            except OSError:
                pass
            _unlock(fd)
            os.close(fd)
            return
        _unlock(fd)
        os.close(fd)
        try:  # fails (sharing violation) while another process has the file open: fine
            os.remove(long_path(self.path))
        except OSError:
            pass

    def __repr__(self) -> str:
        return f"<BuildLock {self.path.name} {'held' if self.held else 'free'}>"
