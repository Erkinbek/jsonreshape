"""File access helpers (docs/ARCHITECTURE.md §4, AGENT_PROMPT §1.7, §3).

* :func:`open_shared` opens source files without locking them: on Windows the file is
  opened through ``CreateFileW`` with ``FILE_SHARE_READ | FILE_SHARE_WRITE |
  FILE_SHARE_DELETE``, so other programs can still write, rename or delete it while it
  is open ("faylni eksklyuziv bloklamaslik").
* :func:`long_path` adds the ``\\\\?\\`` prefix to long Windows paths (>= 240 characters).
* :class:`FileIdentity` / :func:`file_identity` describe a file version (normalized path,
  size, mtime, hash of the first MiB, hash of the last MiB and a hash of 16 sampled pages
  in between) for cache keys and change detection. The tail hash catches the common
  in-place edits that keep the size and restore the modification time (tools that
  preserve times, ``touch -r``): rewritten records at the end of a file; the sampled pages
  (:func:`sample_ranges`: 16 blocks of 4 KiB spread evenly over the middle) catch such an
  edit in the middle only when it touches one of them -- 64 KiB of a 1 GB file, so most
  edits in the middle of a big file that keep size and mtime are still invisible to the
  identity (the reader's span sanity checks catch most of those when the records are
  read, :mod:`jsonreshape.reader`). Checking more of the file would make every open and
  every ``ensure_unchanged`` read more of it.
* :func:`grown_from` / :func:`appended_bytes` tell a file that only *grew* (bytes
  appended to the version an identity describes: an active log) from a changed one: the
  hashes of the identity depend on the content and the size only, so they are computed
  again over the original range of the longer file.
* :func:`sniff` detects gzip, BOMs and BOM-less UTF-16/UTF-32 from the first bytes.
* :func:`prepare_data_file` turns gzip and UTF-16 inputs into a plain UTF-8 *data file*
  in ``appdirs.work_dir(Settings.cache_dir)/prepared`` -- under the configured cache
  folder ("Kesh papkasi") when there is one -- (streaming, bounded memory, progress,
  cancellation, reuse). A copy holds the source as it was identified (bytes appended to
  the source while it is copied are left out). Prepared copies and index files share one
  ``Settings.cache_limit_bytes`` budget (:func:`jsonreshape.index.evict`); copies in use
  or used within the last hour are kept and do not count against it.
* :func:`atomic_write` / :func:`atomic_path` write a new file under a temporary name in
  the target directory and atomically replace the target only on success.
* :func:`read_range` reads an exact byte range.

OS errors are turned into localized :class:`~jsonreshape.errors.JsonReshapeError`
instances (``error.file_not_found``, ``error.permission``, ``fileio.error.in_use`` for a
file another program holds exclusively, ``fileio.error.read_only`` for a read-only output,
``fileio.error.disk_full``, ...); the original :class:`OSError` is kept as ``__cause__``.
Errors while an atomic output is written (disk full, a vanished network drive) name the
output file (:func:`atomic_write`).

Known limitations (Stage 1; the GUI stage revisits both)
========================================================
* gzip and UTF-16 inputs are prepared completely before the first record can be read
  (Document.open waits for the copy: ~0.5 s per 75 MB of gzip measured), unlike plain
  UTF-8 files whose first records appear while the index is still being built.
* While a :class:`~jsonreshape.reader.Document` holds handles on a source file, other
  programs can read, append, overwrite, rename and delete it (shared handles), but the
  "write a temporary file, then ``MoveFileEx``/``os.replace`` it over the original" save
  of some editors fails with "Access denied" until the document is closed: Windows only
  replaces an open file through POSIX-semantics renames. Such changes are detected
  (``Document.ensure_unchanged``), not prevented.
"""

from __future__ import annotations

import codecs
import contextlib
import errno as _errno
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, BinaryIO, Iterable, Iterator, Union

from .appdirs import work_dir
from .errors import FileChangedError, FileFormatError, JsonReshapeError, UserInputError
from .i18n import t
from .tasks import CancelToken, Progress, check

log = logging.getLogger(__name__)

StrPath = Union[str, "os.PathLike[str]"]

__all__ = [
    "CHUNK_BYTES",
    "HEAD_HASH_BYTES",
    "LONG_PATH_THRESHOLD",
    "SAMPLE_BLOCKS",
    "SAMPLE_BYTES",
    "TAIL_HASH_BYTES",
    "CacheEntry",
    "FileIdentity",
    "PreparedFile",
    "Sniff",
    "appended_bytes",
    "atomic_path",
    "atomic_write",
    "encoding_label",
    "evict_entries",
    "file_identity",
    "grown_from",
    "long_path",
    "make_dirs",
    "normalize_path",
    "open_shared",
    "os_error",
    "prepare_data_file",
    "prepared_cache_entries",
    "prepared_dir",
    "prune_prepared",
    "read_range",
    "remove_cache_entry",
    "sample_ranges",
    "shared_opener",
    "sniff",
]

_WINDOWS = sys.platform == "win32"

LONG_PATH_THRESHOLD = 240          # paths this long (UTF-16 units) get the \\?\ prefix
HEAD_HASH_BYTES = 1 << 20          # FileIdentity.head_hash covers the first 1 MiB
TAIL_HASH_BYTES = 1 << 20          # FileIdentity.tail_hash covers the last 1 MiB (after the head)
SAMPLE_BLOCKS = 16                 # FileIdentity.sample_hash: blocks sampled between the head and the tail
SAMPLE_BYTES = 4096                # ... of this many bytes each (page-aligned: one page read per block)
SNIFF_BYTES = 4096                # bytes read by prepare_data_file() for sniffing
CHUNK_BYTES = 1 << 20              # streaming chunk size for gunzip / transcoding
PREPARED_VERSION = 2               # bump when the prepared-file format changes (2: source_bom)
PREPARED_MIN_AGE = 3600.0          # prepared files used within this many seconds are never pruned
_PART_MAX_AGE = 86400.0            # leftover ".part" files older than this are removed

_GZIP_MAGIC = b"\x1f\x8b"


# ----------------------------------------------------------------------------------
# Windows: shared-mode CreateFileW opener
# ----------------------------------------------------------------------------------
if _WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # private instance: own argtypes
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _CreateFileW.restype = wintypes.HANDLE
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)
    _CloseHandle.restype = wintypes.BOOL

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # READ | WRITE | DELETE
    _CREATE_NEW = 1
    _CREATE_ALWAYS = 2
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _TRUNCATE_EXISTING = 5
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    def _win_opener(path: str, flags: int) -> int:
        """``open(..., opener=...)`` hook: CreateFileW with full sharing -> CRT fd."""
        access_bits = flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
        if access_bits == os.O_WRONLY:
            access = _GENERIC_WRITE
        elif access_bits == os.O_RDWR:
            access = _GENERIC_READ | _GENERIC_WRITE
        else:
            access = _GENERIC_READ
        if flags & os.O_CREAT:
            if flags & os.O_EXCL:
                disposition = _CREATE_NEW
            elif flags & os.O_TRUNC:
                disposition = _CREATE_ALWAYS
            else:
                disposition = _OPEN_ALWAYS
        elif flags & os.O_TRUNC:
            disposition = _TRUNCATE_EXISTING
        else:
            disposition = _OPEN_EXISTING
        handle = _CreateFileW(os.fspath(path), access, _FILE_SHARE_ALL, None, disposition,
                              _FILE_ATTRIBUTE_NORMAL, None)
        if handle is None or handle == _INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            # OSError(None, msg, filename, winerror) maps winerror -> errno and subclass
            raise OSError(None, ctypes.FormatError(err), os.fspath(path), err)
        try:
            return msvcrt.open_osfhandle(handle, flags & os.O_APPEND)
        except BaseException:
            _CloseHandle(handle)
            raise

    shared_opener = _win_opener
else:  # pragma: no cover - exercised on POSIX only
    shared_opener = None

# ``shared_opener`` is the opener used by open_shared(); text-mode callers (e.g. the log
# handler) may pass it to ``open(path, mode, encoding=..., opener=shared_opener)``.
# It is ``None`` on non-Windows platforms (plain open() already shares).


# ----------------------------------------------------------------------------------
# paths
# ----------------------------------------------------------------------------------
def _fspath_str(p: StrPath | bytes) -> str:
    s = os.fspath(p)
    if isinstance(s, bytes):
        s = os.fsdecode(s)
    return s


def _strip_long_prefix(s: str) -> str:
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    if s.startswith("\\\\?\\"):
        return s[4:]
    return s


def _utf16_len(s: str) -> int:
    return len(s) + sum(1 for ch in s if ord(ch) > 0xFFFF)


def long_path(p: StrPath | bytes) -> str:
    """Return ``p`` as a ``str`` usable for long paths.

    On Windows, a path whose absolute form has at least :data:`LONG_PATH_THRESHOLD`
    UTF-16 units is returned absolute and normalized with the ``\\\\?\\`` prefix (UNC
    paths ``\\\\server\\share\\...`` become ``\\\\?\\UNC\\server\\share\\...``). Shorter
    paths and paths that already carry a ``\\\\?\\`` / ``\\\\.\\`` prefix are returned
    unchanged. On other platforms this is ``os.fspath``.
    """
    s = _fspath_str(p)
    if not _WINDOWS or s.startswith(("\\\\?\\", "\\\\.\\")):
        return s
    full = os.path.abspath(s)  # GetFullPathNameW: normalizes '/', '.', '..'
    if full.startswith(("\\\\?\\", "\\\\.\\")):
        return full
    if _utf16_len(full) < LONG_PATH_THRESHOLD:
        return s
    if full.startswith("\\\\"):
        return "\\\\?\\UNC\\" + full[2:]
    return "\\\\?\\" + full


def _abspath(p: StrPath | bytes) -> Path:
    """Absolute, normalized path without a long-path prefix (case preserved)."""
    return Path(os.path.abspath(_strip_long_prefix(_fspath_str(p))))


def normalize_path(p: StrPath | bytes) -> str:
    """Normalized absolute path used for identities and cache keys.

    The long-path prefix is removed, the path is made absolute (``.``/``..`` resolved,
    separators normalized) and, on Windows, case-folded with ``os.path.normcase`` (the
    file system's case-insensitive comparison; ``str.casefold`` would also merge names
    such as ``Straße``/``Strasse`` that NTFS keeps apart). Symlinks are not resolved.
    """
    return os.path.normcase(os.path.abspath(_strip_long_prefix(_fspath_str(p))))


def _display(p: StrPath | bytes) -> str:
    return _strip_long_prefix(_fspath_str(p))


def make_dirs(p: StrPath) -> None:
    """``os.makedirs(p, exist_ok=True)`` that also works beyond ``MAX_PATH`` on Windows
    (through :func:`long_path`). Raises :class:`OSError`."""
    os.makedirs(long_path(p), exist_ok=True)


# ----------------------------------------------------------------------------------
# error mapping
# ----------------------------------------------------------------------------------
_NOT_FOUND_WINERRORS = {2, 3, 53, 67, 161}       # file/path not found, bad net path/name, bad pathname
_DISK_FULL_WINERRORS = {39, 112}                  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
_IN_USE_WINERRORS = {32, 33}                      # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
# errors of a write that are about the output file itself (also when they come from a
# caller's write inside atomic_write): disk full, quota exceeded, file too large for the
# file system (FAT32: 4 GB), read-only file system
_WRITE_ERRNOS = {_errno.ENOSPC, _errno.EFBIG, getattr(_errno, "EDQUOT", -1), _errno.EROFS}
_WRITE_WINERRORS = _DISK_FULL_WINERRORS | {223, 1295}   # ERROR_FILE_TOO_LARGE, ERROR_DISK_QUOTA_EXCEEDED
_FILE_ATTRIBUTE_READONLY = 0x1


def _isdir(p: StrPath | bytes) -> bool:
    try:
        return os.path.isdir(long_path(p))
    except (OSError, ValueError):
        return False


def _is_read_only(p: StrPath | bytes) -> bool:
    """Windows: the file has the read-only attribute (it cannot be replaced or overwritten)."""
    if not _WINDOWS:
        return False
    try:
        return bool(getattr(os.stat(long_path(p)), "st_file_attributes", 0) & _FILE_ATTRIBUTE_READONLY)
    except (OSError, ValueError):
        return False


def _is_disk_full(exc: OSError) -> bool:
    return exc.errno == _errno.ENOSPC or getattr(exc, "winerror", None) in _DISK_FULL_WINERRORS


def _is_write_failure(exc: OSError) -> bool:
    """An error of writing data (as opposed to reading the input): see ``_WRITE_ERRNOS``."""
    return exc.errno in _WRITE_ERRNOS or getattr(exc, "winerror", None) in _WRITE_WINERRORS


def os_error(exc: OSError, path: StrPath | bytes, *, writing: bool = False) -> JsonReshapeError:
    """Translate an :class:`OSError` about ``path`` into a localized error.

    ``FileNotFoundError`` -> ``error.file_not_found``; a directory -> ``fileio.error.is_directory``;
    a sharing or lock violation (the file is held by another program, Windows errors 32/33)
    -> ``fileio.error.in_use``; a read-only file -> ``fileio.error.read_only``; any other
    ``PermissionError`` -> ``error.permission`` (these are :class:`UserInputError`); disk
    full -> ``fileio.error.disk_full``; anything else -> ``fileio.error.write`` when
    ``writing`` (the error happened while writing ``path``) or ``fileio.error.open``, with
    the OS message.
    """
    shown = _display(path)
    winerror = getattr(exc, "winerror", None)
    if isinstance(exc, FileNotFoundError) or winerror in _NOT_FOUND_WINERRORS:
        return UserInputError("error.file_not_found", path=shown)
    if winerror in _IN_USE_WINERRORS:
        return UserInputError("fileio.error.in_use", path=shown)
    if isinstance(exc, IsADirectoryError) or (isinstance(exc, PermissionError) and _isdir(path)):
        return UserInputError("fileio.error.is_directory", path=shown)
    if isinstance(exc, PermissionError):
        if _is_read_only(path):
            return UserInputError("fileio.error.read_only", path=shown)
        return UserInputError("error.permission", path=shown)
    if _is_disk_full(exc):
        return JsonReshapeError("fileio.error.disk_full", path=shown)
    detail = exc.strerror or str(exc)
    if writing:
        return JsonReshapeError("fileio.error.write", path=shown, detail=detail)
    return JsonReshapeError("fileio.error.open", path=shown, detail=detail)


# ----------------------------------------------------------------------------------
# opening
# ----------------------------------------------------------------------------------
_BINARY_MODES = frozenset(
    {"rb", "r+b", "rb+", "wb", "w+b", "wb+", "ab", "a+b", "ab+", "xb", "x+b", "xb+"}
)


def open_shared(p: StrPath, mode: str = "rb", buffering: int = -1) -> BinaryIO:
    """Open ``p`` in a binary ``mode`` without denying other processes access.

    Windows: ``CreateFileW`` with ``FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE``
    (other programs may keep writing, renaming or deleting the file; an open handle keeps
    reading the old data), long paths supported. Elsewhere: plain :func:`open`.
    ``buffering`` is passed to :func:`open`. Errors are raised as localized
    :class:`~jsonreshape.errors.JsonReshapeError` (see :func:`os_error`).
    """
    if mode not in _BINARY_MODES:
        raise ValueError(f"open_shared supports binary modes only, got {mode!r}")
    target = long_path(p)
    try:
        if shared_opener is not None:
            return open(target, mode, buffering=buffering, opener=shared_opener)  # noqa: SIM115
        return open(target, mode, buffering=buffering)  # noqa: SIM115
    except OSError as exc:
        raise os_error(exc, p) from exc


def read_range(f: IO[bytes], start: int, end: int) -> bytes:
    """Read exactly the bytes ``[start, end)`` of the open binary file ``f``.

    Raises ``ValueError`` for an invalid range and
    :class:`~jsonreshape.errors.FileChangedError` when the file ends before ``end`` (the
    file was truncated after it was indexed). Not thread-safe: it moves the file position,
    so use one file object per thread (or a lock).
    """
    if start < 0 or end < start:
        raise ValueError(f"invalid byte range [{start}, {end})")
    n = end - start
    if n == 0:
        return b""
    f.seek(start)
    data = f.read(n)
    if data is not None and len(data) == n:
        return data
    parts = [data or b""]
    got = len(parts[0])
    while got < n:
        chunk = f.read(n - got)
        if not chunk:
            break
        parts.append(chunk)
        got += len(chunk)
    if got < n:
        name = getattr(f, "name", "")
        raise FileChangedError(path=_display(name) if isinstance(name, (str, bytes, os.PathLike)) else str(name))
    return b"".join(parts)


# ----------------------------------------------------------------------------------
# identity
# ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class FileIdentity:
    """A version of a file: normalized path, size, modification time and content hashes.

    * ``head_hash``: sha256 of the first ``min(size, HEAD_HASH_BYTES)`` bytes;
    * ``tail_hash``: sha256 of the bytes from ``max(HEAD_HASH_BYTES, size -
      TAIL_HASH_BYTES)`` to the end, ``""`` when the head already covers the whole file;
    * ``sample_hash``: sha256 of the blocks of :func:`sample_ranges` (:data:`SAMPLE_BLOCKS`
      pages of :data:`SAMPLE_BYTES` spread evenly over the part between the head and the
      tail, or that whole part when it is shorter), ``""`` when there is no such part (files
      of at most 2 MiB).

    An identity serialized before a hash existed has ``""`` for it: it never matches a
    current identity of a file big enough to have that hash, so such caches are rebuilt.
    Every hash depends only on the content and on ``size``: :func:`grown_from` hashes the
    same ranges of a file that has grown since.
    """

    path: str          # normalize_path(): absolute, case-folded on Windows
    size: int
    mtime_ns: int
    head_hash: str     # sha256 hex of the first HEAD_HASH_BYTES bytes
    tail_hash: str = ""  # sha256 hex of the last TAIL_HASH_BYTES bytes after the head ("" = none)
    sample_hash: str = ""  # sha256 hex of the sample_ranges() blocks ("" = none)

    @property
    def hashes(self) -> tuple[str, str, str]:
        """``(head_hash, tail_hash, sample_hash)``."""
        return self.head_hash, self.tail_hash, self.sample_hash

    def matches(self, other: FileIdentity | None) -> bool:
        """True when ``other`` describes the same, unchanged file."""
        return (
            other is not None
            and self.path == other.path
            and self.size == other.size
            and self.mtime_ns == other.mtime_ns
            and self.hashes == other.hashes
        )

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "mtime_ns": self.mtime_ns, "head_hash": self.head_hash,
                "tail_hash": self.tail_hash, "sample_hash": self.sample_hash}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileIdentity:
        return cls(
            path=str(data["path"]),
            size=int(data["size"]),
            mtime_ns=int(data["mtime_ns"]),
            head_hash=str(data["head_hash"]),
            tail_hash=str(data.get("tail_hash", "")),
            sample_hash=str(data.get("sample_hash", "")),
        )

    def digest(self) -> str:
        """Stable 32-hex-digit key of this identity (used for cache file names)."""
        canon = json.dumps([self.path, self.size, self.mtime_ns, self.head_hash, self.tail_hash, self.sample_hash],
                           ensure_ascii=True)
        return hashlib.sha256(canon.encode("ascii")).hexdigest()[:32]


def sample_ranges(size: int) -> list[tuple[int, int]]:
    """The byte ranges ``FileIdentity.sample_hash`` covers in a file of ``size`` bytes.

    The part between the head and the tail hash, ``[HEAD_HASH_BYTES, size -
    TAIL_HASH_BYTES)``: whole when it is at most ``SAMPLE_BLOCKS * SAMPLE_BYTES`` long,
    else :data:`SAMPLE_BLOCKS` page-aligned blocks of :data:`SAMPLE_BYTES` centred on
    ``(2k + 1) / (2 * SAMPLE_BLOCKS)`` of it (``k = 0 ..``); ``[]`` when there is no such
    part. Deterministic: the same size gives the same ranges."""
    lo = HEAD_HASH_BYTES
    hi = size - TAIL_HASH_BYTES
    if hi <= lo:
        return []
    span = hi - lo
    if span <= SAMPLE_BLOCKS * SAMPLE_BYTES:
        return [(lo, hi)]
    out = []
    for k in range(SAMPLE_BLOCKS):
        start = lo + span * (2 * k + 1) // (2 * SAMPLE_BLOCKS) - SAMPLE_BYTES // 2
        start -= start % SAMPLE_BYTES
        start = min(max(start, lo), hi - SAMPLE_BYTES)
        out.append((start, start + SAMPLE_BYTES))
    return out


def _read_upto(f: IO[bytes], start: int, n: int) -> bytes:
    """Up to ``n`` bytes of the open file ``f`` from ``start`` (fewer only at its end)."""
    if n <= 0:
        return b""
    f.seek(start)
    data = f.read(n) or b""
    if len(data) >= n:
        return data
    parts = [data]
    got = len(data)
    while got < n:
        chunk = f.read(n - got)
        if not chunk:
            break
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def _content_hashes(f: IO[bytes], size: int) -> tuple[str, str, str]:
    """``(head_hash, tail_hash, sample_hash)`` of the first ``size`` bytes of the open file
    ``f`` (see :class:`FileIdentity`); a file shorter than ``size`` hashes what is there
    (so it never matches). Moves the file position. ``OSError`` passes."""
    head = hashlib.sha256(_read_upto(f, 0, min(size, HEAD_HASH_BYTES))).hexdigest()
    tail = ""
    if size > HEAD_HASH_BYTES:
        start = max(HEAD_HASH_BYTES, size - TAIL_HASH_BYTES)
        tail = hashlib.sha256(_read_upto(f, start, size - start)).hexdigest()
    sample = ""
    ranges = sample_ranges(size)
    if ranges:
        h = hashlib.sha256()
        for s, e in ranges:
            h.update(_read_upto(f, s, e - s))
        sample = h.hexdigest()
    return head, tail, sample


def file_identity(p: StrPath) -> FileIdentity:
    """Identity of the file ``p``: size and mtime from the open handle, the first MiB, the
    last MiB and the sampled pages hashed (see :class:`FileIdentity`; 2 MiB and 16 pages
    read: ~1.2 ms for a multi-GB file in the system cache, measured)."""
    with open_shared(p, "rb", buffering=0) as f:
        try:
            st = os.fstat(f.fileno())
            head_hash, tail_hash, sample_hash = _content_hashes(f, st.st_size)
        except OSError as exc:
            raise os_error(exc, p) from exc
    return FileIdentity(
        path=normalize_path(p),
        size=st.st_size,
        mtime_ns=st.st_mtime_ns,
        head_hash=head_hash,
        tail_hash=tail_hash,
        sample_hash=sample_hash,
    )


def grown_from(f: IO[bytes], identity: FileIdentity) -> bool:
    """True when the open file ``f`` holds the version ``identity`` describes, possibly
    followed by appended bytes: it is at least ``identity.size`` bytes long and the head,
    the tail and the sampled pages *of that version's range* (``[0, identity.size)``) hash
    as they did -- a file that only grew, like a log being written. The modification time
    is not compared (appending changes it). ``f``'s position is restored; ``OSError``
    passes. Like the identity itself this looks at ~2 MiB of the range: an edit elsewhere
    in it goes unnoticed."""
    if os.fstat(f.fileno()).st_size < identity.size:
        return False
    pos = f.tell()
    try:
        hashes = _content_hashes(f, identity.size)
    finally:
        f.seek(pos)
    return hashes == identity.hashes


def appended_bytes(p: StrPath, identity: FileIdentity) -> int | None:
    """How the file now at ``p`` relates to the version ``identity``:

    * ``0`` -- it is that version (same path, size, modification time and hashes:
      ``file_identity(p).matches(identity)``);
    * ``n > 0`` -- ``n`` bytes were appended to that version (:func:`grown_from`);
    * ``None`` -- it is another version: shorter, rewritten (same size, other time or
      content), replaced by a longer file whose first ``identity.size`` bytes differ, or
      another path.

    OS errors are raised as localized errors (:func:`os_error`: ``fileio.error.in_use``
    when another program holds the file exclusively)."""
    if normalize_path(p) != identity.path:
        return None
    with open_shared(p, "rb", buffering=0) as f:
        try:
            st = os.fstat(f.fileno())
            if st.st_size == identity.size:
                if st.st_mtime_ns != identity.mtime_ns:
                    return None
                return 0 if _content_hashes(f, st.st_size) == identity.hashes else None
            if st.st_size < identity.size:
                return None
            return st.st_size - identity.size if grown_from(f, identity) else None
        except OSError as exc:
            raise os_error(exc, p) from exc


# ----------------------------------------------------------------------------------
# sniffing
# ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class Sniff:
    """Result of :func:`sniff`.

    ``encoding``: ``utf-8``, ``utf-8-bom``, ``utf-16-le``, ``utf-16-be``, ``utf-32-le`` or
    ``utf-32-be``; ``bom_len``: length of the byte-order mark (0 when there is none);
    ``gzip``: the bytes are a gzip stream. For gzip the encoding describes nothing
    (``utf-8``/0 placeholders): sniff the decompressed bytes again.
    """

    encoding: str
    bom_len: int
    gzip: bool


_BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),  # before UTF-16 LE (same first two bytes)
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xef\xbb\xbf", "utf-8-bom"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def sniff(head: bytes) -> Sniff:
    """Detect gzip, a BOM, or BOM-less UTF-16/UTF-32 from the first bytes of a file.

    BOM-less detection relies on JSON text starting with an ASCII character (whitespace,
    a structural character, a quote, a digit, ``-`` or a literal), so its first code unit
    shows a characteristic NUL pattern (RFC 4627 §3): ``xx 00 00 00`` UTF-32 LE,
    ``00 00 00 xx`` UTF-32 BE, ``xx 00`` UTF-16 LE (e.g. ``[`` NUL ``{`` NUL), ``00 xx``
    UTF-16 BE. A valid UTF-8 JSON text never contains NUL bytes, so this cannot misfire
    on valid UTF-8 input. Anything else is UTF-8.
    """
    head = bytes(head[:4])
    if head[:2] == _GZIP_MAGIC:
        return Sniff("utf-8", 0, True)
    for bom, encoding in _BOMS:
        if head.startswith(bom):
            return Sniff(encoding, len(bom), False)
    if len(head) >= 4:
        b0, b1, b2, b3 = head
        if b0 and not b1 and not b2 and not b3:
            return Sniff("utf-32-le", 0, False)
        if not b0 and not b1 and not b2 and b3:
            return Sniff("utf-32-be", 0, False)
    if len(head) >= 2:
        b0, b1 = head[0], head[1]
        if b0 and not b1:
            return Sniff("utf-16-le", 0, False)
        if not b0 and b1:
            return Sniff("utf-16-be", 0, False)
    return Sniff("utf-8", 0, False)


# ----------------------------------------------------------------------------------
# atomic writes
# ----------------------------------------------------------------------------------
def _remove_quiet(p: StrPath) -> None:
    try:
        os.remove(long_path(p))
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("could not remove temporary file %s", p, exc_info=True)


def _create_temp(target: Path, suffix: str = ".tmp") -> tuple[int, Path]:
    """Create a new empty temp file next to ``target``; returns ``(fd, path)``."""
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
             | getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_CLOEXEC", 0))
    stem = target.name[:48]
    last_exc: OSError | None = None
    for _ in range(100):
        tmp = target.with_name(f".{stem}.{secrets.token_hex(4)}{suffix}")
        try:
            # mode 0o666 lets the umask decide (mkstemp would force 0o600 on POSIX)
            return os.open(long_path(tmp), flags, 0o666), tmp
        except FileExistsError as exc:
            last_exc = exc
            continue
        except OSError as exc:
            raise os_error(exc, target) from exc
    raise os_error(last_exc or FileExistsError(str(target)), target)  # pragma: no cover


def _replace(src: Path, dst: Path, *, timeout: float = 1.0) -> None:
    """``os.replace`` with retries on Windows (antivirus scanners, indexers and preview
    handlers briefly hold new files). Raises localized errors: a folder in the way, a
    read-only target (``fileio.error.read_only``, no retries) and a target another program
    keeps open (``fileio.error.replace``) are told apart."""
    deadline = time.monotonic() + timeout
    delay = 0.01
    while True:
        try:
            os.replace(long_path(src), long_path(dst))
            return
        except PermissionError as exc:
            if _isdir(dst):
                raise UserInputError("fileio.error.is_directory", path=_display(dst)) from exc
            if _is_read_only(dst):
                raise UserInputError("fileio.error.read_only", path=_display(dst)) from exc
            if not _WINDOWS or time.monotonic() >= deadline:
                raise JsonReshapeError("fileio.error.replace", path=_display(dst)) from exc
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
        except OSError as exc:
            raise os_error(exc, dst, writing=True) from exc


def _fsync_dir(directory: Path) -> None:
    if _WINDOWS:  # directories cannot be fsynced on Windows (and need not be)
        return
    try:  # pragma: no cover - POSIX only
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:  # pragma: no cover
        pass


@contextlib.contextmanager
def atomic_write(
    path: StrPath,
    mode: str = "wb",
    *,
    encoding: str = "utf-8",
    newline: str | None = None,
    fsync: bool = True,
    buffering: int = -1,
) -> Iterator[IO[Any]]:
    """Context manager writing ``path`` atomically.

    Yields a file object for a new temporary file in the target's directory (``mode``
    ``"wb"``, or ``"w"`` for text with ``encoding``/``newline``). When the block finishes
    without an exception the data is flushed, fsynced (``fsync=True``) and the temp file
    replaces ``path`` with ``os.replace``. On any exception, including
    :class:`~jsonreshape.errors.Cancelled` and ``KeyboardInterrupt``, the temp file is
    removed and ``path`` is left untouched.

    An :class:`OSError` of writing the output -- disk full, quota, a file too large for
    the file system, a read-only file system, raised by the block's own writes, or any
    error of the final flush/fsync/close -- is re-raised as a localized error naming
    ``path`` (:func:`os_error`, ``fileio.error.disk_full`` / ``fileio.error.write``).
    Other OS errors inside the block (e.g. reading an input) pass through unchanged.
    """
    if mode not in ("wb", "w"):
        raise ValueError(f"atomic_write supports 'wb' and 'w', got {mode!r}")
    target = _abspath(path)
    fd, tmp = _create_temp(target)
    try:
        if mode == "wb":
            f = os.fdopen(fd, "wb", buffering=buffering)
        else:
            f = os.fdopen(fd, "w", buffering=buffering, encoding=encoding, newline=newline)
    except BaseException:
        os.close(fd)
        _remove_quiet(tmp)
        raise
    try:
        try:
            try:
                yield f
            except OSError as exc:
                if _is_write_failure(exc):
                    raise os_error(exc, target, writing=True) from exc
                raise
            try:
                f.flush()
                if fsync:
                    os.fsync(f.fileno())
                f.close()
            except OSError as exc:
                raise os_error(exc, target, writing=True) from exc
        finally:
            if not f.closed:
                try:
                    f.close()
                except OSError:  # the error that brought us here is more useful
                    log.debug("closing the temporary file %s failed", tmp, exc_info=True)
        _replace(tmp, target)
        _fsync_dir(target.parent)
    except BaseException:
        _remove_quiet(tmp)
        raise


@contextlib.contextmanager
def atomic_path(path: StrPath) -> Iterator[Path]:
    """Like :func:`atomic_write` for libraries that need a *file name* (sqlite3,
    xlsxwriter): yields the path of a new empty temp file next to ``path``; on success it
    replaces ``path``, on any exception it is removed. Write failures (disk full, ...)
    raised as :class:`OSError` inside the block are localized like in
    :func:`atomic_write`."""
    target = _abspath(path)
    fd, tmp = _create_temp(target)
    os.close(fd)
    try:
        try:
            yield tmp
        except OSError as exc:
            if _is_write_failure(exc):
                raise os_error(exc, target, writing=True) from exc
            raise
        _replace(tmp, target)
        _fsync_dir(target.parent)
    except BaseException:
        _remove_quiet(tmp)
        raise


# ----------------------------------------------------------------------------------
# data file preparation (gzip / UTF-16 -> UTF-8)
# ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class PreparedFile:
    """Result of :func:`prepare_data_file`.

    * ``data_path``: the UTF-8 file to scan (the source itself for UTF-8 input, otherwise
      a file in ``work_dir(cache_dir)/prepared``); absolute.
    * ``encoding``: encoding of the *source* content (``utf-8``, ``utf-8-bom``,
      ``utf-16-le``, ``utf-16-be``; for gzip the encoding of the decompressed content).
    * ``compression``: ``"gzip"`` or ``None``.
    * ``bom_len``: offset of the first JSON byte in ``data_path`` (3 for a UTF-8 BOM in an
      uncompressed source, else 0 -- prepared files never contain a BOM).
    * ``identity``: :class:`FileIdentity` of the source file.
    * ``warnings``: i18n keys (e.g. ``fileio.warning.gzip_truncated``).
    * ``source_bom``: prepared copies only: length of the byte-order mark that the
      (decompressed) source content starts with and the copy leaves out (2 for UTF-16, 3
      for UTF-8); maps offsets in the copy back to the source (the reader's error offsets).

    Iterating yields ``(data_path, encoding, compression)`` -- the tuple form used in
    docs/ARCHITECTURE.md §4.
    """

    data_path: Path
    encoding: str
    compression: str | None
    bom_len: int
    identity: FileIdentity
    warnings: tuple[str, ...] = ()
    source_bom: int = 0

    @property
    def converted(self) -> bool:
        """True when ``data_path`` is a prepared copy rather than the source file."""
        return self.compression is not None or self.encoding.startswith("utf-16")

    def __iter__(self) -> Iterator[Any]:
        return iter((self.data_path, self.encoding, self.compression))


class _GzipReader:
    """Streaming, multi-member gzip decompressor with bounded output per :meth:`read`.

    Unlike :class:`gzip.GzipFile` it keeps every decompressed byte of a truncated stream
    (``truncated`` is set instead of raising) and stops at non-gzip trailing data
    (``trailing`` is set). Corrupt data raises :class:`zlib.error`.
    """

    def __init__(self, raw: BinaryIO, in_chunk: int = CHUNK_BYTES) -> None:
        self._raw = raw
        self._in_chunk = in_chunk
        self._d = zlib.decompressobj(31)
        self._pending = b""
        self._finished = False
        self.consumed = 0       # compressed bytes read from ``raw``
        self.truncated = False
        self.trailing = False

    def _fill(self) -> bytes:
        data = self._raw.read(self._in_chunk)
        self.consumed += len(data)
        return data

    def _next_member(self, rest: bytes) -> bool:
        """Start the next member after a completed one; False at the clean end."""
        while True:
            rest = rest.lstrip(b"\x00")  # zero padding between/after members is allowed
            if rest:
                break
            rest = self._fill()
            if not rest:
                return False
        if len(rest) < 2:
            rest += self._fill()
        if rest[:2] != _GZIP_MAGIC:
            self.trailing = True
            return False
        self._d = zlib.decompressobj(31)
        self._pending = rest
        return True

    def read(self, size: int) -> bytes:
        while not self._finished:
            d = self._d
            if d.eof:
                if not self._next_member(d.unused_data):
                    self._finished = True
                    return b""
                continue
            data = d.unconsumed_tail or self._pending or self._fill()
            self._pending = b""
            if not data:
                self._finished = True
                self.truncated = True
                return d.flush()
            out = d.decompress(data, size)
            if out:
                return out
        return b""


class _Snapshot:
    """``read(n)`` of the open source ``raw`` that ends after ``limit`` bytes: a prepared
    copy holds the version that was identified, not bytes appended while it is copied."""

    def __init__(self, raw: BinaryIO, limit: int) -> None:
        self._raw = raw
        self._left = max(0, int(limit))

    def read(self, size: int) -> bytes:
        if self._left <= 0:
            return b""
        data = self._raw.read(min(size, self._left)) or b""
        self._left -= len(data)
        return data


class _PlainReader:
    def __init__(self, raw: BinaryIO) -> None:
        self._raw = raw
        self.consumed = 0
        self.truncated = False
        self.trailing = False

    def read(self, size: int) -> bytes:
        data = self._raw.read(size)
        self.consumed += len(data)
        return data


_ENCODING_LABELS = {
    "utf-8": "UTF-8",
    "utf-8-bom": "UTF-8 (BOM)",
    "utf-16-le": "UTF-16 LE",
    "utf-16-be": "UTF-16 BE",
    "utf-32-le": "UTF-32 LE",
    "utf-32-be": "UTF-32 BE",
}


def encoding_label(encoding: str) -> str:
    """Display name of an encoding returned by :func:`sniff` (``UTF-16 LE``, ...)."""
    return _ENCODING_LABELS.get(encoding, encoding)


def _utf32_error(path: StrPath, encoding: str) -> FileFormatError:
    return FileFormatError("fileio.error.utf32", path=_display(path), encoding=encoding_label(encoding))


def prepared_dir(cache_dir: StrPath | None = None) -> Path:
    """``appdirs.work_dir(cache_dir)/prepared`` (created on demand): under the configured
    cache folder (``Settings.cache_dir``) when there is one, else ``tmp_dir()/prepared``."""
    d = work_dir(cache_dir) / "prepared"
    try:
        make_dirs(d)
    except OSError as exc:
        raise os_error(exc, d, writing=True) from exc
    return d


_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


@contextlib.contextmanager
def _key_lock(key: str, cancel: CancelToken | None) -> Iterator[None]:
    """Serialize preparation of the same identity inside this process."""
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    while not lock.acquire(timeout=0.05):
        check(cancel)
    try:
        yield
    finally:
        lock.release()


def _read_meta(meta_path: Path, data_path: Path, identity: FileIdentity) -> PreparedFile | None:
    """The prepared file described by ``meta_path`` when it is complete and current."""
    try:
        with open(long_path(meta_path), "rb") as f:
            meta = json.loads(f.read(1 << 20).decode("utf-8"))
        if not isinstance(meta, dict) or meta.get("version") != PREPARED_VERSION:
            return None
        if not FileIdentity.from_dict(meta["identity"]).matches(identity):
            return None
        if os.stat(long_path(data_path)).st_size != int(meta["data_size"]):
            return None
        encoding = str(meta["encoding"])
        compression = meta.get("compression")
        warnings = tuple(str(w) for w in meta.get("warnings", ()))
        source_bom = int(meta.get("source_bom", 0))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return PreparedFile(data_path, encoding, compression, 0, identity, warnings, source_bom)


def _touch(p: Path) -> None:
    try:
        os.utime(long_path(p))
    except OSError:
        pass


# Prepared data files currently read by open documents of this process (normalized path ->
# reference count). prune_prepared() never deletes them, whatever their age.
_IN_USE_LOCK = threading.Lock()
_IN_USE: dict[str, int] = {}


def retain_prepared(path: StrPath) -> None:
    """Mark a prepared data file as in use by an open document (it will not be pruned).

    Also refreshes its ``.meta`` mtime so other processes see it as recently used.
    Every call must be balanced by :func:`release_prepared`.
    """
    p = Path(os.fspath(path))
    norm = normalize_path(p)
    with _IN_USE_LOCK:
        _IN_USE[norm] = _IN_USE.get(norm, 0) + 1
    _touch(p.with_suffix(".meta"))


def release_prepared(path: StrPath) -> None:
    """Undo one :func:`retain_prepared` call."""
    norm = normalize_path(path)
    with _IN_USE_LOCK:
        n = _IN_USE.get(norm, 0) - 1
        if n > 0:
            _IN_USE[norm] = n
        else:
            _IN_USE.pop(norm, None)


def _prepared_in_use(norm: str) -> bool:
    with _IN_USE_LOCK:
        return norm in _IN_USE


def _report(progress: Progress | None, done: int) -> None:
    if progress is not None:
        progress.update(done)


def prepare_data_file(
    path: StrPath,
    *,
    progress: Progress | None = None,
    cancel: CancelToken | None = None,
    settings: Any = None,
) -> PreparedFile:
    """Make ``path`` scannable as UTF-8 and describe how (see :class:`PreparedFile`).

    * plain UTF-8 (with or without BOM): the source itself is the data file;
    * gzip (magic ``1f 8b``, whatever the extension) and/or UTF-16 LE/BE (BOM or BOM-less):
      the content is streamed (1 MiB chunks, incremental decoder, bounded memory) into
      ``prepared_dir(settings.cache_dir)/<identity digest>.json`` (under the configured
      cache folder when there is one) as UTF-8 without BOM, written under a temporary
      ``.part`` name and renamed when complete; a ``.meta`` file records the source
      identity. Only the ``identity.size`` bytes of the identified version are copied
      (bytes another program appends meanwhile are not: the copy is that snapshot). A
      complete prepared file whose identity matches is reused. Progress is reported in
      *source* bytes (compressed bytes for gzip); cancellation removes the partial file.
      After building, least-recently-used cache files -- prepared copies and index files
      together -- are removed to keep the removable ones within
      ``settings.cache_limit_bytes`` (:func:`jsonreshape.index.evict`).
    * UTF-32 -> :class:`~jsonreshape.errors.FileFormatError` (``fileio.error.utf32``).

    ``settings``: a :class:`~jsonreshape.settings.Settings` (default: the process-wide one).

    Invalid UTF-16 data -> ``FileFormatError`` (``fileio.error.decode``, with the byte
    offset in the decoded stream); a corrupt gzip stream -> ``fileio.error.gzip``. A
    *truncated* gzip stream (or non-gzip data after the last member) is not an error: what
    could be decompressed is kept and a warning key is added, so the reader can still open
    the records before the cut (AGENT_PROMPT §14 test 13). The whole copy is written
    before this returns (see "Known limitations" in the module docstring).
    """
    check(cancel)
    src = _abspath(path)
    identity = file_identity(src)
    with open_shared(src, "rb") as f:
        try:
            head = f.read(SNIFF_BYTES)
        except OSError as exc:
            raise os_error(exc, src) from exc
    sn = sniff(head)
    if not sn.gzip:
        if sn.encoding in ("utf-8", "utf-8-bom"):
            return PreparedFile(src, sn.encoding, None, sn.bom_len, identity)
        if sn.encoding.startswith("utf-32"):
            raise _utf32_error(src, sn.encoding)
    if settings is None:
        from .settings import get_settings

        settings = get_settings()
    if progress is not None:
        if progress.total is None:
            progress.set_total(identity.size)
        if not progress.message:
            progress.set_message(t("fileio.progress.decompress" if sn.gzip else "fileio.progress.transcode"))

    key = identity.digest()
    cache_dir = getattr(settings, "cache_dir", None)
    pdir = prepared_dir(cache_dir)
    data_path = pdir / f"{key}.json"
    meta_path = pdir / f"{key}.meta"
    with _key_lock(key, cancel):
        cached = _read_meta(meta_path, data_path, identity)
        if cached is not None:
            _touch(meta_path)
            log.debug("reusing prepared file %s for %s", data_path, src)
            _report(progress, identity.size)
            return cached
        result = _build_prepared(src, identity, sn, data_path, meta_path, progress, cancel)
    try:
        from .appdirs import index_dir
        from .index import evict

        # one budget for the whole cache: prepared copies and index files (LRU)
        evict(int(getattr(settings, "cache_limit_bytes", 0)), keep=(data_path,), directory=index_dir(cache_dir),
              prepared_dir=pdir)
    except Exception:  # pruning is best effort
        log.warning("pruning the cache failed", exc_info=True)
    return result


def _build_prepared(
    src: Path,
    identity: FileIdentity,
    sn: Sniff,
    data_path: Path,
    meta_path: Path,
    progress: Progress | None,
    cancel: CancelToken | None,
) -> PreparedFile:
    started = time.perf_counter()
    compression = "gzip" if sn.gzip else None
    tmp = data_path.with_name(f"{data_path.stem}.{os.getpid()}.{secrets.token_hex(4)}.part")
    warnings: list[str] = []
    encoding = sn.encoding
    source_bom = 0
    try:
        with open_shared(src, "rb", buffering=0) as raw, open(long_path(tmp), "xb") as out:
            snapshot: Any = _Snapshot(raw, identity.size)   # the identified version only
            reader: _GzipReader | _PlainReader = _GzipReader(snapshot) if sn.gzip else _PlainReader(snapshot)
            try:
                chunk = reader.read(CHUNK_BYTES)
                inner = sniff(chunk) if sn.gzip else sn
                encoding = inner.encoding
                if encoding.startswith("utf-32"):
                    raise _utf32_error(src, encoding)
                skip = source_bom = inner.bom_len
                decoder = None
                if encoding.startswith("utf-16"):
                    decoder = codecs.getincrementaldecoder(encoding)("strict")
                fed = 0  # bytes (after the BOM) handed to the decoder so far
                while chunk:
                    check(cancel)
                    if skip:
                        cut = min(skip, len(chunk))
                        chunk = chunk[cut:]
                        skip -= cut
                    if decoder is not None:
                        pending = len(decoder.buffer)
                        try:
                            text = decoder.decode(chunk)
                        except UnicodeDecodeError as exc:
                            offset = inner.bom_len + fed - pending + exc.start
                            raise FileFormatError("fileio.error.decode", path=_display(src),
                                                  encoding=encoding_label(encoding), offset=offset) from None
                        fed += len(chunk)
                        out.write(text.encode("utf-8"))
                    else:
                        out.write(chunk)
                    _report(progress, reader.consumed)
                    chunk = reader.read(CHUNK_BYTES)
                if decoder is not None and not reader.truncated:
                    pending = len(decoder.buffer)
                    try:
                        out.write(decoder.decode(b"", final=True).encode("utf-8"))
                    except UnicodeDecodeError:
                        offset = inner.bom_len + fed - pending
                        raise FileFormatError("fileio.error.decode", path=_display(src),
                                              encoding=encoding_label(encoding), offset=offset) from None
            except zlib.error as exc:
                raise FileFormatError("fileio.error.gzip", path=_display(src), detail=str(exc)) from None
            if reader.truncated:
                warnings.append("fileio.warning.gzip_truncated")
                log.warning("gzip stream of %s is truncated; keeping %d decompressed bytes", src, out.tell())
            if reader.trailing:
                warnings.append("fileio.warning.gzip_trailing")
                log.warning("gzip stream of %s has trailing non-gzip data (ignored)", src)
            out.flush()
            os.fsync(out.fileno())
            data_size = out.tell()
        check(cancel)
        try:
            _replace(tmp, data_path)
        except JsonReshapeError:
            # e.g. another process replaced and still maps the same prepared file
            existing = _read_meta(meta_path, data_path, identity)
            if existing is None:
                raise
            _remove_quiet(tmp)
            _report(progress, identity.size)
            return existing
        meta = {
            "version": PREPARED_VERSION,
            "identity": identity.to_dict(),
            "source": str(src),
            "encoding": encoding,
            "compression": compression,
            "data_size": data_size,
            "source_bom": source_bom,
            "warnings": warnings,
            "created": time.time(),
        }
        with atomic_write(meta_path) as mf:
            # ASCII JSON: a file name with a lone surrogate (possible on NTFS) stays encodable
            mf.write(json.dumps(meta, ensure_ascii=True, indent=1).encode("ascii"))
    except OSError as exc:
        _remove_quiet(tmp)
        disk_full = _is_disk_full(exc)
        # disk full: name the prepared file (where the space is needed); else the source
        raise os_error(exc, tmp if disk_full else src) from exc
    except BaseException:
        _remove_quiet(tmp)
        raise
    _report(progress, identity.size)
    elapsed = time.perf_counter() - started
    log.info("prepared %s (%s%s) -> %s: %d -> %d bytes in %.2f s", src, encoding_label(encoding),
             ", gzip" if compression else "", data_path, identity.size, data_size, elapsed)
    return PreparedFile(data_path, encoding, compression, 0, identity, tuple(warnings), source_bom)


@dataclass
class CacheEntry:
    """One item of the shared cache budget (:func:`jsonreshape.index.evict`).

    ``last_used``: ``time.time()`` of the last use (LRU order); ``size``: bytes it frees;
    ``paths``: removed in this order -- when the first one cannot be removed (in use) the
    entry is kept; ``protected``: never removed and not counted against the budget (a
    prepared copy an open document of this process reads, one used within
    ``PREPARED_MIN_AGE`` seconds, or a file in ``keep``): see :func:`evict_entries`.
    """

    last_used: float
    size: int
    paths: tuple[Path, ...]
    protected: bool = False


def prepared_cache_entries(directory: StrPath | None = None, *, keep: Iterable[StrPath] = (),
                           min_age: float = PREPARED_MIN_AGE) -> list[CacheEntry]:
    """The prepared copies in ``directory`` (default :func:`prepared_dir`) as
    :class:`CacheEntry` items. A copy is "used" when it is built or reused (its ``.meta``
    mtime). Housekeeping on the way: leftover ``.part`` files older than a day and data
    files without metadata older than ``min_age`` are removed."""
    pdir = Path(directory) if directory is not None else prepared_dir()
    now = time.time()
    keep_norm = {normalize_path(k) for k in keep}
    try:
        names = os.listdir(long_path(pdir))
    except OSError:
        return []
    name_set = set(names)
    entries: list[CacheEntry] = []
    for name in names:
        p = pdir / name
        try:
            st = os.stat(long_path(p))
        except OSError:
            continue
        if name.endswith(".part"):
            if now - st.st_mtime > _PART_MAX_AGE:
                _remove_quiet(p)
            continue
        if name.endswith(".meta"):
            data = p.with_suffix(".json")
            try:
                size = os.stat(long_path(data)).st_size
            except OSError:
                size = 0
            norm = normalize_path(data)
            protected = now - st.st_mtime < min_age or norm in keep_norm or _prepared_in_use(norm)
            entries.append(CacheEntry(st.st_mtime, size, (data, p), protected))
        elif name.endswith(".json") and name[: -len(".json")] + ".meta" not in name_set:
            norm = normalize_path(p)
            if now - st.st_mtime > min_age and norm not in keep_norm and not _prepared_in_use(norm):
                _remove_quiet(p)
    return entries


def remove_cache_entry(entry: CacheEntry) -> bool:
    """Remove the files of ``entry``; ``False`` (nothing removed) when its first file is
    in use."""
    first = entry.paths[0]
    try:
        os.remove(long_path(first))
    except FileNotFoundError:
        pass
    except OSError:
        log.debug("cache file %s is in use; not removed", first)
        return False
    for p in entry.paths[1:]:
        _remove_quiet(p)
    return True


def evict_entries(entries: Iterable[CacheEntry], limit_bytes: int) -> int:
    """Remove least-recently-used ``entries`` until the removable ones hold at most
    ``limit_bytes``; returns the number of bytes freed (the rule of the shared cache
    budget, :func:`jsonreshape.index.evict`).

    Only removable entries count against the budget: protected ones (files in ``keep``,
    prepared copies open documents of this process read or used within
    ``PREPARED_MIN_AGE`` seconds) and entries whose first file cannot be removed because
    another program or process has it open (Windows: a memory-mapped index) are left out
    of the total and kept. So a few big, recently used prepared copies never make an
    eviction delete every unrelated index file: the cache holds at most ``limit_bytes`` of
    removable files *plus* the protected and in-use ones, which a later eviction removes
    once they are neither."""
    removable = [e for e in entries if not e.protected]
    total = sum(e.size for e in removable)
    if total <= limit_bytes:
        return 0
    freed = 0
    for entry in sorted(removable, key=lambda e: e.last_used):
        if total <= limit_bytes:
            break
        total -= entry.size   # removed now, or in use: out of the budget either way
        if not remove_cache_entry(entry):
            continue
        freed += entry.size
        log.info("removed cache file %s (%d bytes)", entry.paths[0], entry.size)
    return freed


def prune_prepared(
    limit_bytes: int,
    *,
    keep: tuple[StrPath, ...] | list[StrPath] = (),
    min_age: float = PREPARED_MIN_AGE,
    directory: StrPath | None = None,
) -> int:
    """Delete least-recently-used prepared files of ``directory`` (default
    :func:`prepared_dir`) until the removable ones hold at most ``limit_bytes``
    (:func:`evict_entries`); returns the number of bytes freed. (Documents use the
    shared budget of :func:`jsonreshape.index.evict`, which counts index files as well.)

    Files used within the last ``min_age`` seconds, files in ``keep`` and files open
    documents of this process read are never deleted and do not count. Leftover ``.part``
    files older than a day and data files without metadata older than ``min_age`` are
    removed as well.
    """
    return evict_entries(prepared_cache_entries(directory, keep=keep, min_age=min_age), limit_bytes)
