"""Background fetch + bounded cache for the rows a view shows (docs/ARCHITECTURE.md §11.3).

A :class:`RowLoader` belongs to one view. The view asks for the values of its visible rows
with :meth:`RowLoader.get` (cached value, or ``None`` + the key is queued) and announces its
visible window with :meth:`RowLoader.request`. One daemon worker thread per loader calls
``fetch(keys, cancel) -> {key: value}`` in batches; results go into a thread-safe LRU cache
and :attr:`RowLoader.loaded` tells the view (in the GUI thread) to repaint.

Scheduling
----------
* **Newest window first**: ``request(keys)`` *replaces* the wanted set with the missing keys
  of the new window, in the given order. Keys of older windows that were not fetched yet are
  dropped (a batch already in flight finishes and its results are cached). ``get()`` misses
  join the current window (appended).
* ``prefetch(keys)`` replaces the prefetch set; prefetched keys are fetched only while no
  wanted key is pending.
* Batches hold at most ``batch`` keys. ``cancel`` (a :class:`~jsonreshape.tasks.CancelToken`)
  fires on :meth:`~RowLoader.close` and when :meth:`~RowLoader.invalidate` makes the whole
  batch stale, so long fetches can stop early.

Cache
-----
LRU (``OrderedDict`` + lock) bounded by ``capacity`` entries and, when ``max_bytes`` is set,
by the sum of ``sizeof(value)`` (the newest entry is always kept, even when it alone exceeds
``max_bytes``). Values may be anything except ``None`` (``None`` means "not loaded"; a
``None`` returned by ``fetch`` is not cached). Keys must be hashable.

* ``account(key, nbytes)`` adds to the size of a cached value the owner enlarged later
  (evicting older entries when the total exceeds ``max_bytes``).
* ``invalidate()`` forgets all cached values; ``invalidate(keys)`` forgets some. Results of
  fetches that started before (generation / per-key staleness check) are discarded; the keys
  of the current window are fetched again automatically.
* ``clear()`` forgets everything -- the cache, the window, the queued and prefetched keys --
  for keys that now mean something else (the view shows another document): running fetches
  are discarded and nothing is fetched until the view asks again. (``invalidate()`` would
  fetch the old window again -- against the new data.)
* A failing ``fetch`` stores :class:`LoadFailed` for every key of the batch (logged once per
  loader); later ``get()`` calls return it until the key is invalidated.
  ``invalidate_failed()`` forgets every cached :class:`LoadFailed` (the keys of the window are
  fetched again): the data may have become readable (e.g. the index of the document grew).

Signals
-------
``loaded`` is emitted in the GUI thread and coalesced: the worker posts at most one queued
notification at a time; the next one only after the GUI thread handled it. Worker threads
never touch widgets.
"""

from __future__ import annotations

import logging
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable, Mapping
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal, Slot

from jsonreshape.errors import Cancelled, JsonReshapeError
from jsonreshape.tasks import CancelToken

log = logging.getLogger(__name__)

__all__ = ["LoadFailed", "RowLoader"]

FetchFn = Callable[[list, CancelToken], "Mapping[Any, Any] | None"]


class LoadFailed:
    """Cached value of a key whose fetch raised ``error``; ``text`` is a short localized
    message (``str(error)`` for :class:`~jsonreshape.errors.JsonReshapeError`, else the
    exception type name)."""

    __slots__ = ("error", "text")

    def __init__(self, error: BaseException, text: str | None = None) -> None:
        self.error = error
        self.text = text if text is not None else self.describe(error)

    @staticmethod
    def describe(error: BaseException) -> str:
        if isinstance(error, JsonReshapeError):
            return str(error)
        return type(error).__name__

    def __repr__(self) -> str:
        return f"LoadFailed({self.error!r}, {self.text!r})"


class _State:
    """Everything the worker thread shares with the GUI thread (guarded by ``lock``).

    Kept apart from the QObject so the worker never keeps the loader alive and stops when
    the loader is destroyed.
    """

    def __init__(self, fetch: FetchFn, capacity: int, batch: int, max_bytes: int | None,
                 sizeof: Callable[[Any], int] | None, name: str) -> None:
        self.fetch = fetch
        self.capacity = capacity
        self.batch = batch
        self.max_bytes = max_bytes
        self.sizeof = sizeof
        self.name = name
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.cache: OrderedDict[Hashable, Any] = OrderedDict()
        self.sizes: dict[Hashable, int] = {}  # only with sizeof
        self.nbytes = 0
        self.window: OrderedDict[Hashable, None] = OrderedDict()  # newest request (+ get misses)
        self.wanted: OrderedDict[Hashable, None] = OrderedDict()  # to fetch, in order
        self.prefetch: OrderedDict[Hashable, None] = OrderedDict()
        self.inflight: frozenset = frozenset()  # keys of the batch being fetched
        self.stale: set = set()  # in-flight keys invalidated after the fetch started
        self.gen = 0  # bumped by invalidate()
        self.token: CancelToken | None = None  # of the batch in flight
        self.closed = False
        self.thread: threading.Thread | None = None
        self.emit_pending = False
        self.failure_logged = False
        self.emitter: Callable[[], None] = lambda: None  # set by RowLoader: coalesced ``loaded``

    # -- helpers (lock held) ---------------------------------------------------------
    def coming(self, key: Hashable) -> bool:
        """A valid result for ``key`` is on its way."""
        return key in self.inflight and key not in self.stale

    def want(self, key: Hashable) -> bool:
        """Queue a missing key of the current window; True when it was added."""
        if key in self.wanted or key in self.cache or self.coming(key):
            return False
        self.prefetch.pop(key, None)
        self.wanted[key] = None
        return True

    def trim_window(self) -> None:
        limit = 2 * self.capacity
        while len(self.window) > limit:
            key, _ = self.window.popitem(last=False)
            self.wanted.pop(key, None)

    def forget(self, key: Hashable) -> None:
        self.cache.pop(key, None)
        self.nbytes -= self.sizes.pop(key, 0)

    def store(self, key: Hashable, value: Any, size: int) -> None:
        cache = self.cache
        if self.sizeof is not None:
            self.nbytes += size - self.sizes.get(key, 0)
            self.sizes[key] = size
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > self.capacity:
            self.evict_oldest()
        self.evict_for_bytes(key)

    def evict_for_bytes(self, keep: Hashable) -> None:
        """While over ``max_bytes``: evict the oldest entries that are neither ``keep`` nor
        in the current window. Rows on screen are never evicted for their size -- with a
        budget smaller than the visible rows the loader would otherwise fetch and evict them
        forever; the window itself is bounded (2 x capacity keys)."""
        if self.max_bytes is None or self.nbytes <= self.max_bytes:
            return
        window = self.window
        for old in [k for k in self.cache if k != keep and k not in window]:
            if self.nbytes <= self.max_bytes:
                break
            self.forget(old)

    def evict_oldest(self) -> None:
        old, _ = self.cache.popitem(last=False)
        if self.sizeof is not None:
            self.nbytes -= self.sizes.pop(old, 0)

    def wake(self) -> None:
        """Start the worker lazily (or again, should it have died) / wake it up."""
        if self.closed:
            return
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(
                target=_worker, args=(self, self.emitter), name=f"RowLoader-{self.name or id(self)}",
                daemon=True)
            self.thread.start()
        self.cond.notify()

    def shutdown(self, *_args: Any) -> None:
        """Stop the worker without waiting (also connected to ``QObject.destroyed``)."""
        with self.lock:
            self.closed = True
            self.window.clear()
            self.wanted.clear()
            self.prefetch.clear()
            if self.token is not None:
                self.token.cancel()
            self.cond.notify_all()


def _worker(state: _State, emit: Callable[[], None]) -> None:
    """Worker thread: fetch batches until the loader is closed."""
    while True:
        with state.cond:
            while not state.closed and not state.wanted and not state.prefetch:
                state.cond.wait()
            if state.closed:
                return
            queue = state.wanted if state.wanted else state.prefetch
            keys: list = []
            while queue and len(keys) < state.batch:
                key, _ = queue.popitem(last=False)
                if key not in state.cache:
                    keys.append(key)
            if not keys:
                continue
            gen = state.gen
            token = CancelToken()
            state.token = token
            state.inflight = frozenset(keys)
            fetch = state.fetch

        result: Mapping[Any, Any] | None = None
        error: BaseException | None = None
        try:
            result = fetch(list(keys), token)
            if result is not None and not isinstance(result, Mapping):
                raise TypeError(f"fetch returned {type(result).__name__}, expected a mapping")
        except Cancelled as exc:
            if not token.cancelled:  # not ours: an error like any other
                error = exc
        except Exception as exc:  # noqa: BLE001 - reported as LoadFailed values
            error = exc

        try:
            stored = _finish(state, keys, gen, result, error)
        except Exception:  # noqa: BLE001 - never let the worker die silently
            log.exception("RowLoader %s: internal error", state.name)
            with state.lock:  # never leave keys "in flight" forever
                state.inflight = frozenset()
                state.token = None
                state.stale.difference_update(keys)
            stored = False
        if stored:
            emit()


def _finish(state: _State, keys: list, gen: int, result: Mapping[Any, Any] | None,
            error: BaseException | None) -> bool:
    """Store the results of one batch; True when something new is in the cache."""
    values: dict[Hashable, Any] = {}
    if error is not None:
        failed = LoadFailed(error)
        values = {key: failed for key in keys}
    elif result:
        for key in keys:
            value = result.get(key)
            if value is not None:
                values[key] = value
    sizes: dict[Hashable, int] = {}
    if state.sizeof is not None:  # outside the lock: sizeof may be slow
        sizeof = state.sizeof
        for key, value in values.items():
            try:
                sizes[key] = int(sizeof(value))
            except Exception:  # noqa: BLE001
                log.exception("RowLoader %s: sizeof failed", state.name)
                sizes[key] = 0

    log_error = False
    stored = False
    with state.lock:
        state.inflight = frozenset()
        state.token = None
        if state.closed:
            state.stale.clear()
            return False
        all_stale = gen != state.gen
        requeue = []
        for key in keys:
            # (a cancelled fetch is always stale: tokens fire only in invalidate() / close())
            if all_stale or key in state.stale:
                # invalidated meanwhile: discard, fetch again when still in the window
                if key in state.window and key not in state.cache:
                    requeue.append(key)
                continue
            value = values.get(key)
            if value is not None:
                state.store(key, value, sizes.get(key, 0))
                stored = True
        state.stale.difference_update(keys)
        for key in reversed(requeue):
            if key not in state.wanted:
                state.prefetch.pop(key, None)
                state.wanted[key] = None
                state.wanted.move_to_end(key, last=False)
        if error is not None and not all_stale and not state.failure_logged:
            state.failure_logged = True
            log_error = True
    if log_error:
        log.warning("RowLoader %s: fetch of %d key(s) failed", state.name, len(keys), exc_info=error)
    elif error is not None:
        log.debug("RowLoader %s: fetch failed again: %r", state.name, error)
    return stored


class RowLoader(QObject):
    """Background loader with a bounded LRU cache for the rows of one view (GUI thread API)."""

    loaded = Signal()  # coalesced: some requested values became available
    _wake = Signal()  # worker -> GUI thread (queued)

    def __init__(self, fetch: FetchFn, *, capacity: int = 4096, batch: int = 64,
                 max_bytes: int | None = None, sizeof: Callable[[Any], int] | None = None,
                 name: str = "", parent: QObject | None = None) -> None:
        super().__init__(parent)
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if batch < 1:
            raise ValueError("batch must be >= 1")
        if max_bytes is not None and sizeof is None:
            raise ValueError("max_bytes needs a sizeof function")
        self.setObjectName(f"RowLoader:{name}" if name else "RowLoader")
        state = _State(fetch, capacity, batch, max_bytes, sizeof, name)
        self._state = state
        self_ref = weakref.ref(self)

        def emit() -> None:  # worker thread
            with state.lock:
                if state.emit_pending or state.closed:
                    return
                state.emit_pending = True
            obj = self_ref()
            if obj is None:
                state.shutdown()
                return
            try:
                obj._wake.emit()
            except RuntimeError:  # the C++ object is gone
                state.shutdown()

        state.emitter = emit
        self._wake.connect(self._on_wake, Qt.ConnectionType.QueuedConnection)
        self.destroyed.connect(state.shutdown)

    @Slot()
    def _on_wake(self) -> None:
        state = self._state
        with state.lock:
            state.emit_pending = False
            closed = state.closed
        if not closed:
            self.loaded.emit()

    # ------------------------------------------------------------------ API
    def get(self, key: Hashable) -> Any | None:
        """Cached value (LRU touch), or ``None``: the key joins the current window and is
        queued (unless closed or already on its way)."""
        state = self._state
        with state.lock:
            cache = state.cache
            if key in cache:
                cache.move_to_end(key)
                return cache[key]
            if state.closed:
                return None
            if key not in state.window:
                state.window[key] = None
                state.trim_window()
            if state.want(key):
                state.wake()
        return None

    def peek(self, key: Hashable) -> Any | None:
        """Cached value without LRU touch and without queuing."""
        state = self._state
        with state.lock:
            return state.cache.get(key)

    def request(self, keys: Iterable[Hashable]) -> None:
        """The visible window changed: fetch its missing keys (in this order) before anything
        else; keys of older windows that are not in flight are dropped. A window larger than
        ``capacity`` is truncated to its first ``capacity`` keys."""
        keys = list(keys)
        state = self._state
        with state.lock:
            if state.closed:
                return
            window: OrderedDict[Hashable, None] = OrderedDict.fromkeys(keys)
            while len(window) > state.capacity:
                window.popitem()
            state.window = window
            state.wanted = OrderedDict()
            cache = state.cache
            for key in window:
                if key in cache:
                    cache.move_to_end(key)  # visible: keep it
                else:
                    state.want(key)
            if state.wanted:
                state.wake()

    def prefetch(self, keys: Iterable[Hashable]) -> None:
        """Replace the low-priority set: fetched only while no requested key is pending."""
        keys = list(keys)
        state = self._state
        with state.lock:
            if state.closed:
                return
            prefetch: OrderedDict[Hashable, None] = OrderedDict()
            for key in keys:
                if key in state.cache or key in state.wanted or state.coming(key):
                    continue
                prefetch[key] = None
                if len(prefetch) >= state.capacity:
                    break
            state.prefetch = prefetch
            if prefetch:
                state.wake()

    def invalidate(self, keys: Iterable[Hashable] | None = None) -> None:
        """Forget all (``None``) or some cached values. Results of fetches already running
        are discarded for those keys; keys of the current window are fetched again."""
        keys = None if keys is None else list(keys)
        state = self._state
        with state.lock:
            if keys is None:
                state.cache.clear()
                state.sizes.clear()
                state.nbytes = 0
                state.gen += 1
                state.stale.update(state.inflight)
                if state.token is not None:
                    state.token.cancel()
                for key in state.window:
                    state.want(key)
            else:
                gone = set(keys)
                for key in gone:
                    state.forget(key)
                    if key in state.inflight:
                        state.stale.add(key)
                for key in state.window:  # fetched again in window order
                    if key in gone:
                        state.want(key)
                if state.inflight and state.token is not None and state.inflight <= state.stale:
                    state.token.cancel()  # nothing of this batch is still useful
            if state.wanted or state.prefetch:
                state.wake()

    def clear(self) -> None:
        """Forget everything: cached values, the window, the queued and prefetched keys (the
        keys mean something else now, e.g. the view shows another document). Results of
        fetches running now are discarded; nothing is fetched until the view asks again."""
        state = self._state
        with state.lock:
            if state.closed:
                return
            state.window = OrderedDict()
            state.wanted = OrderedDict()
            state.prefetch = OrderedDict()
            state.cache.clear()
            state.sizes.clear()
            state.nbytes = 0
            state.gen += 1
            state.stale.update(state.inflight)
            if state.token is not None:
                state.token.cancel()

    def invalidate_failed(self) -> int:
        """Forget every cached :class:`LoadFailed` (the keys of the window are fetched again):
        what failed may be readable now. Returns how many were forgotten."""
        state = self._state
        with state.lock:
            if state.closed:
                return 0
            gone = [key for key, value in state.cache.items() if isinstance(value, LoadFailed)]
            for key in gone:
                state.forget(key)
            if not gone:
                return 0
            dropped = set(gone)
            for key in state.window:     # fetched again in window order
                if key in dropped:
                    state.want(key)
            if state.wanted:
                state.wake()
            return len(gone)

    def account(self, key: Hashable, nbytes: int) -> None:
        """Add ``nbytes`` to the accounted size of the cached value of ``key`` (the owner
        added data to it, e.g. a cell computed later on the GUI thread) and touch it; while
        the total exceeds ``max_bytes`` the oldest *other* entries are evicted. No-op without
        ``sizeof`` or when ``key`` is not cached."""
        state = self._state
        with state.lock:
            if state.sizeof is None or key not in state.cache:
                return
            state.sizes[key] = state.sizes.get(key, 0) + int(nbytes)
            state.nbytes += int(nbytes)
            state.cache.move_to_end(key)
            state.evict_for_bytes(key)

    def cached_bytes(self) -> int:
        """The accounted size of everything cached (0 without ``sizeof``)."""
        state = self._state
        with state.lock:
            return state.nbytes

    def pending(self) -> int:
        """Number of distinct keys queued or being fetched."""
        state = self._state
        with state.lock:
            extra = sum(1 for key in state.inflight if key not in state.wanted and key not in state.prefetch)
            return len(state.wanted) + len(state.prefetch) + extra

    def close(self) -> None:
        """Stop the worker (waits up to 2 s); idempotent. Cached values stay readable."""
        state = self._state
        state.shutdown()
        thread = state.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)
            if thread.is_alive():
                log.warning("RowLoader %s: worker still busy after close()", state.name)

    @property
    def name(self) -> str:
        return self._state.name
