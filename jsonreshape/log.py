"""Logging setup (docs/ARCHITECTURE.md §4, AGENT_PROMPT §11 "Loglar").

:func:`setup_logging` attaches a rotating UTF-8 log file ``logs_dir()/jsonreshape.log``
(5 MiB per file, 3 backups: ``.1`` .. ``.3``) to the *root* logger, so messages of every
module and library end up in one place. Records carry a timestamp, the process id, the
level, the thread name and the logger name. The call is idempotent: repeating it
reconfigures the level/console output instead of adding handlers (and moves the file
handler when the log directory changed, e.g. a new ``JSONRESHAPE_HOME``).

The log file is opened through :data:`jsonreshape.fileio.shared_opener` on Windows, so a
second running instance can still rotate (rename) it; a rollover that fails anyway is
skipped for a minute instead of spamming "--- Logging error ---" on stderr.

Logging never gets in the way of the work: when the log file cannot be opened or written
(an unwritable data folder, a full disk) file logging is switched off once for the rest
of the process (one line goes to the console handler when there is one), and a
:class:`logging.NullHandler` on the root logger keeps Python's ``logging.lastResort``
from printing warnings and tracebacks on stderr -- also when :func:`setup_logging`
itself fails (the data folder falls back to a temporary folder first, see
:mod:`jsonreshape.appdirs`).

:func:`install_excepthooks` logs uncaught exceptions of the main thread
(``sys.excepthook``, which Qt also uses for exceptions in slots) and of worker threads
(``threading.excepthook``) before delegating to the previous hooks.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .appdirs import logs_dir

__all__ = [
    "BACKUP_COUNT",
    "LOG_FILE_NAME",
    "MAX_BYTES",
    "install_excepthooks",
    "log_path",
    "setup_logging",
    "shutdown_logging",
]

LOG_FILE_NAME = "jsonreshape.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
FILE_FORMAT = "%(asctime)s.%(msecs)03d %(process)d %(levelname)-8s [%(threadName)s] %(name)s: %(message)s"
CONSOLE_FORMAT = "%(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_ROLLOVER_RETRY_SECONDS = 60.0

_log = logging.getLogger(__name__)
_lock = threading.RLock()
_file_handler: _SharedRotatingFileHandler | None = None
_console_handler: logging.Handler | None = None
_null_handler: logging.NullHandler | None = None


def log_path() -> Path:
    """Path of the current log file (``logs_dir()/jsonreshape.log``)."""
    return logs_dir() / LOG_FILE_NAME


def _install_null_handler() -> None:
    """A NullHandler on the root logger: records nobody handles are dropped instead of
    going to ``logging.lastResort`` (which prints them, tracebacks included, on stderr)."""
    global _null_handler
    root = logging.getLogger()
    with _lock:
        if _null_handler is None or _null_handler not in root.handlers:
            _null_handler = logging.NullHandler()
            root.addHandler(_null_handler)


class _SharedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that opens the file shared, survives failed rollovers and
    switches itself off (once, quietly) when the file cannot be opened or written."""

    _jsonreshape_handler = True

    def __init__(self, filename: str | os.PathLike[str]) -> None:
        self._rollover_failed_at = 0.0
        self.disabled_reason: str | None = None   # set when file logging was switched off
        super().__init__(
            filename,
            mode="a",
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
            errors="backslashreplace",
        )

    def emit(self, record: logging.LogRecord) -> None:
        if self.disabled_reason is not None:
            return
        super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API
        """Instead of printing "--- Logging error ---" with a traceback for every record:
        an OS error (the file cannot be opened or written) switches file logging off for
        good; other errors (e.g. a bad format string in one call) go the standard way."""
        exc = sys.exc_info()[1]
        if not isinstance(exc, OSError):
            super().handleError(record)
            return
        self.disabled_reason = f"{type(exc).__name__}: {exc}"
        try:
            if self.stream is not None:
                self.stream.close()
        except Exception:  # pragma: no cover - the stream is broken anyway
            pass
        self.stream = None  # type: ignore[assignment]
        console = _console_handler
        if console is not None:
            try:
                console.handle(logging.makeLogRecord({
                    "name": __name__, "levelno": logging.WARNING, "levelname": "WARNING",
                    "msg": "file logging disabled: %s (%s)", "args": (self.baseFilename, self.disabled_reason),
                }))
            except Exception:  # pragma: no cover
                pass

    def _open(self):  # noqa: ANN202 - logging API
        from .fileio import long_path, shared_opener

        return open(  # noqa: SIM115 - the handler owns the stream
            long_path(self.baseFilename),
            self.mode,
            encoding=self.encoding,
            errors=self.errors,
            opener=shared_opener,
        )

    def shouldRollover(self, record: logging.LogRecord) -> int:  # noqa: N802 - logging API
        if self._rollover_failed_at and time.monotonic() - self._rollover_failed_at < _ROLLOVER_RETRY_SECONDS:
            return 0
        return super().shouldRollover(record)

    def doRollover(self) -> None:  # noqa: N802 - logging API
        try:
            super().doRollover()
            self._rollover_failed_at = 0.0
        except OSError:
            # e.g. another process keeps an old backup open; keep writing to this file
            self._rollover_failed_at = time.monotonic()
            if self.stream is None:
                self.stream = self._open()


class _ConsoleHandler(logging.StreamHandler):
    _jsonreshape_handler = True

    def __init__(self) -> None:
        super().__init__(sys.stderr)


def _parse_level(level: int | str) -> int:
    if isinstance(level, bool):
        raise ValueError(f"invalid log level: {level!r}")
    if isinstance(level, int):
        return level
    name = str(level).strip().upper()
    value = logging.getLevelName(name)
    if isinstance(value, int):
        return value
    if name.isdigit():
        return int(name)
    raise ValueError(f"invalid log level: {level!r}")


def setup_logging(level: int | str = "INFO", console: bool = False) -> Path:
    """Configure logging to the rotating log file (and optionally stderr).

    ``level`` is a level name (``"DEBUG"``, ``"INFO"``, ...) or number; it is applied to
    the root logger and to the handlers. ``console=True`` adds (``False`` removes) a
    stderr handler. Returns the log file path. Safe to call repeatedly and from any thread.
    Never fails because of the file system: when no log folder can be used at all, file
    logging is skipped (the returned path is the one that could not be used).
    """
    lvl = _parse_level(level)
    global _file_handler, _console_handler
    _install_null_handler()
    try:
        path = log_path()
    except Exception as exc:  # no usable data folder at all (appdirs already fell back)
        path = Path(LOG_FILE_NAME)
        _log.debug("no log folder: %s", exc)
        usable = False
    else:
        usable = True
    started = False
    with _lock:
        root = logging.getLogger()
        fh = _file_handler
        if fh is not None and (
            not usable or fh not in root.handlers
            or os.path.normcase(fh.baseFilename) != os.path.normcase(os.path.abspath(path))
        ):
            root.removeHandler(fh)
            fh.close()
            fh = None
            _file_handler = None
        if fh is None and usable:
            fh = _SharedRotatingFileHandler(path)
            fh.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
            root.addHandler(fh)
            _file_handler = fh
            started = True
        if fh is not None:
            fh.setLevel(lvl)

        if console:
            if _console_handler is None or _console_handler not in root.handlers:
                _console_handler = _ConsoleHandler()
                _console_handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))
                root.addHandler(_console_handler)
            _console_handler.setLevel(lvl)
        elif _console_handler is not None:
            root.removeHandler(_console_handler)
            _console_handler.close()
            _console_handler = None

        root.setLevel(lvl)
        logging.captureWarnings(True)
    if started:
        _log.info("logging started: jsonreshape %s, Python %s, %s, pid %d",
                  _app_version(), platform.python_version(), _os_text(), os.getpid())
    return path


def _os_text() -> str:
    """Short OS description for the log header. On Windows ``platform.platform()`` runs a
    WMI query (~40-50 ms, noticeable at GUI start-up); ``sys.getwindowsversion()`` is
    instant."""
    if sys.platform == "win32":
        v = sys.getwindowsversion()
        return f"Windows {v.major}.{v.minor}.{v.build}"
    return platform.platform()


def shutdown_logging() -> None:
    """Remove and close the handlers added by :func:`setup_logging` (idempotent)."""
    global _file_handler, _console_handler, _null_handler
    with _lock:
        root = logging.getLogger()
        for h in (_file_handler, _console_handler, _null_handler):
            if h is not None:
                root.removeHandler(h)
                h.close()
        _file_handler = None
        _console_handler = None
        _null_handler = None
        logging.captureWarnings(False)


def _app_version() -> str:
    try:
        from . import __version__

        return __version__
    except Exception:  # pragma: no cover
        return "?"


# ----------------------------------------------------------------------------------
# uncaught exceptions
# ----------------------------------------------------------------------------------
def install_excepthooks() -> None:
    """Log uncaught exceptions (main thread and worker threads), then call the previous
    hooks. ``KeyboardInterrupt`` and ``SystemExit`` in threads are not logged.
    Installing twice is a no-op."""
    hook_log = logging.getLogger("jsonreshape.uncaught")

    if not getattr(sys.excepthook, "_jsonreshape_hook", False):
        previous = sys.excepthook

        def excepthook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
            if not issubclass(exc_type, KeyboardInterrupt):
                try:
                    hook_log.critical("uncaught exception", exc_info=(exc_type, exc, tb))
                except Exception:  # pragma: no cover - never fail inside the hook
                    pass
            previous(exc_type, exc, tb)

        excepthook._jsonreshape_hook = True  # type: ignore[attr-defined]
        sys.excepthook = excepthook

    if not getattr(threading.excepthook, "_jsonreshape_hook", False):
        previous_thread = threading.excepthook

        def thread_excepthook(args: threading.ExceptHookArgs) -> None:
            if args.exc_type is not None and not issubclass(args.exc_type, (SystemExit, KeyboardInterrupt)):
                name = args.thread.name if args.thread is not None else "?"
                try:
                    hook_log.critical("uncaught exception in thread %s", name,
                                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
                except Exception:  # pragma: no cover
                    pass
            previous_thread(args)

        thread_excepthook._jsonreshape_hook = True  # type: ignore[attr-defined]
        threading.excepthook = thread_excepthook
