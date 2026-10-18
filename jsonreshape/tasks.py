"""Background work primitives: cancellation, progress (%, rate, ETA) and threads.

All long operations accept ``progress: Progress | None`` and ``cancel: CancelToken | None``.
They call ``cancel.check()`` regularly (raises :class:`~jsonreshape.errors.Cancelled`) and
``progress.update(done)`` with the amount of work done (bytes by default).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .errors import Cancelled

log = logging.getLogger(__name__)


class CancelToken:
    """Thread-safe cancellation flag."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


def check(cancel: CancelToken | None) -> None:
    """``cancel.check()`` that accepts ``None``."""
    if cancel is not None and cancel._event.is_set():
        raise Cancelled()


@dataclass(frozen=True)
class ProgressInfo:
    done: int
    total: int | None
    fraction: float | None  # 0..1, None when total unknown
    rate: float  # units per second
    eta: float | None  # seconds remaining, None when unknown
    elapsed: float
    message: str
    unit: str
    finished: bool = False


ProgressCallback = Callable[[ProgressInfo], None]


class Progress:
    """Progress tracker with throttled callbacks.

    ``callback`` is invoked from the worker thread at most every ``min_interval`` seconds
    (and always on :meth:`finish`). GUI code must marshal it to the UI thread itself.
    """

    def __init__(
        self,
        total: int | None = None,
        *,
        callback: ProgressCallback | None = None,
        unit: str = "bytes",
        message: str = "",
        min_interval: float = 0.1,
    ) -> None:
        self._lock = threading.Lock()
        self.total = total
        self.done = 0
        self.unit = unit
        self.message = message
        self.callback = callback
        self.min_interval = min_interval
        self.start_time = time.perf_counter()
        self._last_emit = 0.0
        self.finished = False
        # parent mapping (for child progresses)
        self._parent: Progress | None = None
        self._p_start = 0.0
        self._p_end = 1.0

    # -- updates ---------------------------------------------------------------------
    def update(self, done: int) -> None:
        with self._lock:
            self.done = done
        self._maybe_emit()

    def advance(self, n: int) -> None:
        with self._lock:
            self.done += n
        self._maybe_emit()

    def set_total(self, total: int | None) -> None:
        with self._lock:
            self.total = total
        self._maybe_emit()

    def set_message(self, message: str) -> None:
        with self._lock:
            self.message = message
        self._maybe_emit(force=True)

    def finish(self) -> None:
        with self._lock:
            if self.total is not None and self.done < self.total:
                self.done = self.total
            self.finished = True
        self._maybe_emit(force=True)

    def restart_clock(self) -> None:
        with self._lock:
            self.start_time = time.perf_counter()

    # -- reporting -------------------------------------------------------------------
    def info(self) -> ProgressInfo:
        with self._lock:
            done, total = self.done, self.total
            elapsed = max(1e-9, time.perf_counter() - self.start_time)
            rate = done / elapsed if elapsed > 0 else 0.0
            fraction = None
            eta = None
            if total:
                fraction = min(1.0, max(0.0, done / total))
                if rate > 0 and done < total:
                    eta = (total - done) / rate
                elif done >= total:
                    eta = 0.0
            return ProgressInfo(
                done=done,
                total=total,
                fraction=fraction,
                rate=rate,
                eta=eta,
                elapsed=elapsed,
                message=self.message,
                unit=self.unit,
                finished=self.finished,
            )

    def _maybe_emit(self, force: bool = False) -> None:
        now = time.perf_counter()
        if not force and now - self._last_emit < self.min_interval:
            return
        self._last_emit = now
        info = self.info()
        if self.callback is not None:
            try:
                self.callback(info)
            except Exception:  # never let a UI callback kill the worker
                log.exception("progress callback failed")
        if self._parent is not None and info.fraction is not None:
            span = self._p_end - self._p_start
            parent = self._parent
            if parent.total:
                parent.update(int(parent.total * (self._p_start + span * info.fraction)))

    def child(self, start_fraction: float, end_fraction: float, *, total: int | None = None,
              unit: str | None = None, message: str | None = None) -> Progress:
        """A sub-progress whose completion maps onto ``[start_fraction, end_fraction]`` of
        this progress (which must have a total)."""
        sub = Progress(total, unit=unit or self.unit, message=message if message is not None else self.message,
                       min_interval=self.min_interval)
        sub._parent = self
        sub._p_start = start_fraction
        sub._p_end = end_fraction
        return sub


class BackgroundTask:
    """Run ``fn(*args, progress=..., cancel=..., **kwargs)`` in a daemon thread."""

    def __init__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "",
        progress: Progress | None = None,
        cancel: CancelToken | None = None,
        **kwargs: Any,
    ) -> None:
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.name = name or getattr(fn, "__name__", "task")
        self.progress = progress if progress is not None else Progress()
        self.cancel_token = cancel if cancel is not None else CancelToken()
        self._result: Any = None
        self.exception: BaseException | None = None
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_done: Callable[[BackgroundTask], None] | None = None

    def start(self) -> BackgroundTask:
        if self._thread is not None:
            raise RuntimeError("task already started")
        self._thread = threading.Thread(target=self._run, name=f"jsonreshape-{self.name}", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self._result = self.fn(*self.args, progress=self.progress, cancel=self.cancel_token, **self.kwargs)
        except BaseException as exc:  # noqa: BLE001 - stored and re-raised by result()
            self.exception = exc
            if not isinstance(exc, Cancelled):
                log.exception("background task %s failed", self.name)
        finally:
            self._done.set()
            if self.on_done is not None:
                try:
                    self.on_done(self)
                except Exception:
                    log.exception("on_done callback failed")

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def join(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def result(self, timeout: float | None = None) -> Any:
        if not self._done.wait(timeout):
            raise TimeoutError(self.name)
        if self.exception is not None:
            raise self.exception
        return self._result

    def cancel(self) -> None:
        self.cancel_token.cancel()


def format_bytes(n: float) -> str:
    """Human readable size: 1.5 KB, 2.0 MB (decimal units, as shown in the UI). A value
    that rounds to 1000 of a unit is shown in the next unit (999 950 -> ``1.0 MB``, not
    ``1000.0 KB``)."""
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(n)
    if value != value or value in (float("inf"), float("-inf")):
        return "? B"
    for i, unit in enumerate(units):
        last = i == len(units) - 1
        if unit == "B":
            if abs(value) < 1000 or last:
                return f"{int(value)} B"
        elif last or abs(round(value, 1)) < 1000:
            return f"{value:.1f} {unit}"
        value /= 1000.0
    return f"{value:.1f} TB"  # pragma: no cover


def format_duration(seconds: float | None) -> str:
    """``5.2 s`` / ``42 s`` / ``2:05`` / ``1:02:05`` (``?`` for ``None``, NaN and infinity).
    A value that rounds to 60 s is shown as ``1:00`` (not ``60 s``)."""
    if seconds is None or seconds != seconds or seconds in (float("inf"), float("-inf")):
        return "?"
    seconds = max(0.0, seconds)
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 59.5:
        return f"{seconds:.0f} s"
    minutes, sec = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}:{sec:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{sec:02d}"


def parse_size(text: str) -> int:
    """Parse sizes like ``500MB``, ``2 GB``, ``1048576``, ``64k`` (binary multiples for k/M/G)."""
    s = text.strip().upper().replace(" ", "")
    if not s:
        raise ValueError("empty size")
    mult = 1
    for suffix, factor in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10),
                           ("T", 1 << 40), ("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10), ("B", 1)):
        if s.endswith(suffix):
            mult = factor
            s = s[: -len(suffix)]
            break
    # exact decimal arithmetic: sizes above 2**53 bytes keep every digit ("9007199254740993")
    try:
        number = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"not a number: {text!r}") from None
    if not number.is_finite():  # "nan", "inf"
        raise ValueError("size must be a finite number")
    if number < 0:
        raise ValueError("negative size")
    if number.adjusted() > 40:  # "1e400", "1e300TB": far beyond any real size
        raise ValueError("size must be a finite number")
    return int(number * mult)
