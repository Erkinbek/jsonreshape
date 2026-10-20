"""Standard input as a command-line INPUT (docs/ARCHITECTURE.md §10 "Standard input").

Every reader of this program works on a seekable file on disk (the index stores byte ranges,
gzip / UTF-16 inputs are converted to a cached UTF-8 copy by :mod:`jsonreshape.fileio`), so a
pipe cannot be read directly. :func:`spool_stdin` copies standard input to a *spool file* --
``stdin-<pid>-<random>.json`` in the work folder the caller names (``<JSONRESHAPE_HOME>/tmp``,
or the ``tmp`` folder of a configured cache folder: :func:`jsonreshape.appdirs.work_dir`),
created with :func:`tempfile.mkstemp` (``O_EXCL``, mode 0600 where the OS has modes) -- and the
CLI then opens that file like any named input: ``--format`` / ``--record-path`` apply to it,
gzip data on the pipe is recognized by its magic bytes and converted by the normal path.

* The copy is streamed in :data:`CHUNK_BYTES` pieces (constant memory: one piece at a time),
  always from the *binary* stream (``sys.stdin.buffer``: no newline translation on Windows).
* Progress is reported in bytes read (``progress.update``); the total is unknown
  (``Progress.total`` stays ``None``), so a CLI progress line shows the bytes so far and the rate.
* Cancellation (``cancel.check()`` after every piece), ``KeyboardInterrupt`` and any error remove
  the partial spool file before the exception propagates.
* Disk space: before the first piece and after every :data:`FREE_SPACE_CHECK_EVERY` bytes the free
  space of the spool folder must be at least the bytes read so far plus :data:`FREE_SPACE_RESERVE`
  (the length of a pipe is unknown: at least as much again is assumed); otherwise
  ``JsonReshapeError("fileio.error.disk_full")`` names the folder. A write that fails (the disk
  really full) is reported through :func:`jsonreshape.fileio.os_error` as well.
* :func:`stdin_is_tty` tells a terminal (or a missing stdin) from a pipe / redirected file: the
  CLI refuses ``-`` then (``cli.error.stdin_tty``) instead of waiting for keyboard input.

The result, a :class:`SpooledStdin`, knows the path and the size; :meth:`SpooledStdin.remove`
deletes the file (retried briefly on Windows while a scanner holds it; a file that cannot be
removed is logged, never raised). The CLI removes it when the command ends, also on an error.
The i18n keys of the messages live in the ``cli`` section (``cli.error.stdin_*``): standard
input is a feature of the command line only.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from .errors import JsonReshapeError
from .fileio import free_space, long_path, os_error
from .tasks import CancelToken, Progress, check

log = logging.getLogger(__name__)

__all__ = [
    "CHUNK_BYTES",
    "FREE_SPACE_CHECK_EVERY",
    "FREE_SPACE_RESERVE",
    "SPOOL_PREFIX",
    "SPOOL_SUFFIX",
    "STDIN_MARK",
    "SpooledStdin",
    "spool_stdin",
    "stdin_is_tty",
]

STDIN_MARK = "-"                      # the INPUT that means standard input
MiB = 1 << 20
CHUNK_BYTES = MiB                     # one piece read and written at a time
FREE_SPACE_RESERVE = 64 * MiB         # free space required beyond the bytes read so far
FREE_SPACE_CHECK_EVERY = 256 * MiB    # the free space is checked at every multiple of this
SPOOL_PREFIX = "stdin-"               # spool file name: stdin-<pid>-<random>.json
SPOOL_SUFFIX = ".json"
_REMOVE_TRIES = 5                     # Windows: a scanner may hold the new file for a moment
_REMOVE_DELAY = 0.05


@dataclass
class SpooledStdin:
    """Standard input copied to the file ``path`` (``size`` bytes)."""

    path: Path
    size: int

    def remove(self) -> bool:
        """Delete the spool file. ``True`` when it is gone (also when it was gone already); a file
        that cannot be removed (another program still holds it) is logged and left: ``False``."""
        return _remove(self.path)


def stdin_is_tty(stream: Any = None) -> bool:
    """``True`` when standard input (``stream``: ``sys.stdin`` by default) is a terminal or does
    not exist (``None``: a process started without one), ``False`` for a pipe or a redirected
    file. Never raises."""
    s = sys.stdin if stream is None else stream
    if s is None:
        return True
    try:
        return bool(s.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _binary_stream(stream: Any) -> Any:
    """The binary side of ``stream`` (``sys.stdin.buffer``); a text stream without one is read as
    text and encoded (in-process callers that replaced ``sys.stdin``)."""
    buffer = getattr(stream, "buffer", None)
    return buffer if buffer is not None else stream


def _read_piece(stream: Any, n: int) -> bytes:
    """Up to ``n`` bytes of ``stream`` (``b""`` at its end). ``read1`` -- what one read of the pipe
    gives -- keeps a slow producer's progress visible; text is encoded as UTF-8."""
    read1 = getattr(stream, "read1", None)
    data = read1(n) if read1 is not None else stream.read(n)
    if data is None:                           # a non-blocking stream without data: wait for it
        data = stream.read(n) or b""
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogateescape")
    return data


def _check_free_space(folder: Path, read: int) -> None:
    free = free_space(folder)
    if free is not None and free < read + FREE_SPACE_RESERVE:
        log.warning("spooling standard input stopped: %d bytes read, %d free in %s", read, free, folder)
        raise JsonReshapeError("fileio.error.disk_full", path=str(folder))


def spool_stdin(folder: os.PathLike[str] | str, *, stream: Any = None, progress: Progress | None = None,
                cancel: CancelToken | None = None) -> SpooledStdin:
    """Copy standard input (``stream``: ``sys.stdin`` by default, read through its binary buffer) to
    a new spool file in ``folder`` (which must exist) and return it as a :class:`SpooledStdin`.

    See the module docstring: constant memory, progress in bytes (no total), cancellation and
    errors remove the partial file, the free-space rule. OS errors are raised as localized
    :class:`~jsonreshape.errors.JsonReshapeError` instances (:func:`jsonreshape.fileio.os_error`).
    """
    check(cancel)
    source = _binary_stream(sys.stdin if stream is None else stream)
    directory = Path(os.fspath(folder))
    _check_free_space(directory, 0)
    try:
        fd, name = tempfile.mkstemp(prefix=f"{SPOOL_PREFIX}{os.getpid()}-", suffix=SPOOL_SUFFIX,
                                    dir=long_path(directory))
    except OSError as exc:
        raise os_error(exc, directory, writing=True) from exc
    path = Path(name)
    read = 0
    next_check = FREE_SPACE_CHECK_EVERY
    started = time.perf_counter()
    try:
        out: IO[bytes] = os.fdopen(fd, "wb", buffering=CHUNK_BYTES)
    except BaseException:
        os.close(fd)
        _remove(path)
        raise
    try:
        try:
            while True:
                check(cancel)
                piece = _read_piece(source, CHUNK_BYTES)
                if not piece:
                    break
                out.write(piece)
                read += len(piece)
                if progress is not None:
                    progress.update(read)
                if read >= next_check:
                    _check_free_space(directory, read)
                    next_check = (read // FREE_SPACE_CHECK_EVERY + 1) * FREE_SPACE_CHECK_EVERY
            out.flush()
            out.close()
        except OSError as exc:
            raise os_error(exc, path, writing=True) from exc
    except BaseException:
        if not out.closed:
            try:
                out.close()
            except OSError:  # the error that brought us here is more useful
                log.debug("closing the spool file %s failed", path, exc_info=True)
        _remove(path)
        raise
    if progress is not None:
        progress.finish()
    log.info("standard input spooled to %s: %d bytes in %.2f s", path, read, time.perf_counter() - started)
    return SpooledStdin(path, read)


def _remove(path: Path) -> bool:
    target = long_path(path)
    for attempt in range(_REMOVE_TRIES):
        try:
            os.remove(target)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if attempt + 1 < _REMOVE_TRIES:
                time.sleep(_REMOVE_DELAY)
                continue
            log.warning("could not remove the spool file %s", path, exc_info=True)
            return False
        except OSError:
            log.warning("could not remove the spool file %s", path, exc_info=True)
            return False
    return False  # pragma: no cover
