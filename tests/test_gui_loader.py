"""RowLoader (jsonreshape/gui/loader.py, docs/ARCHITECTURE.md §11.3).

Ordering tests use a fetch function that records its calls and blocks on an event, so the
worker's progress is deterministic.
"""

from __future__ import annotations

import gc
import logging
import random
import threading
import time

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import QObject  # noqa: E402

from jsonreshape.errors import Cancelled, UserInputError  # noqa: E402
from jsonreshape.gui.loader import LoadFailed, RowLoader  # noqa: E402
from jsonreshape.i18n import t  # noqa: E402

pytestmark = pytest.mark.gui


class GatedFetch:
    """Records every call; each call blocks until ``gate`` is set (or, when ``honor_cancel``,
    until its cancel token fires: then it raises ``Cancelled``)."""

    def __init__(self, value=lambda k: f"v{k}", *, honor_cancel: bool = True) -> None:
        self.value = value
        self.honor_cancel = honor_cancel
        self.calls: list[list] = []
        self.tokens: list = []
        self.started = threading.Event()
        self.gate = threading.Event()
        self.lock = threading.Lock()

    def __call__(self, keys, cancel):
        with self.lock:
            self.calls.append(list(keys))
            self.tokens.append(cancel)
        values = {k: self.value(k) for k in keys}  # computed when the fetch starts
        self.started.set()
        deadline = time.monotonic() + 10
        while not self.gate.wait(0.005):
            if self.honor_cancel and cancel.cancelled:
                raise Cancelled()
            if time.monotonic() > deadline:
                raise TimeoutError("gate never opened")
        return values


@pytest.fixture
def make_loader(qapp):
    loaders: list[RowLoader] = []
    gates: list[threading.Event] = []

    def make(fetch, **kwargs) -> RowLoader:
        loader = RowLoader(fetch, **kwargs)
        loaders.append(loader)
        if isinstance(fetch, GatedFetch):
            gates.append(fetch.gate)
        return loader

    yield make
    for gate in gates:
        gate.set()
    for loader in loaders:
        loader.close()


def quick(keys, cancel):
    return {k: f"v{k}" for k in keys}


def cached_keys(loader: RowLoader, keys) -> list:
    return [k for k in keys if loader.peek(k) is not None]


# --------------------------------------------------------------------------- basics


def test_values_arrive_and_loaded_fires(qtbot, make_loader):
    loader = make_loader(quick, name="basic")
    assert loader.get(1) is None  # miss: queued
    with qtbot.waitSignal(loader.loaded, timeout=3000):
        pass
    assert loader.get(1) == "v1"
    with qtbot.waitSignal(loader.loaded, timeout=3000):
        loader.request([2, 3, 4])
    qtbot.waitUntil(lambda: cached_keys(loader, [2, 3, 4]) == [2, 3, 4], timeout=3000)
    assert loader.get(3) == "v3" and loader.pending() == 0
    assert loader.peek(99) is None and loader.pending() == 0  # peek never queues
    assert loader.name == "basic"


def test_constructor_validation(qapp):
    for kwargs in ({"capacity": 0}, {"batch": 0}, {"max_bytes": 10}):
        with pytest.raises(ValueError):
            RowLoader(quick, **kwargs)


def test_none_values_are_not_cached(qtbot, make_loader):
    calls = []

    def fetch(keys, cancel):
        calls.append(list(keys))
        if 100 in keys:
            return None  # "nothing loaded"
        return {k: (None if k % 2 else k) for k in keys}

    loader = make_loader(fetch)
    loader.request([0, 1, 2, 3])
    qtbot.waitUntil(lambda: loader.pending() == 0, timeout=3000)
    assert cached_keys(loader, range(4)) == [0, 2]
    loader.request([100])  # fetch returns None: nothing cached, no retry loop
    qtbot.waitUntil(lambda: loader.pending() == 0, timeout=3000)
    qtbot.wait(50)
    assert loader.peek(100) is None
    assert calls == [[0, 1, 2, 3], [100]]


# --------------------------------------------------------------------------- scheduling


def test_newest_window_first_and_stale_keys_dropped(qtbot, make_loader):
    fetch = GatedFetch()
    loader = make_loader(fetch, batch=2)
    loader.request([0, 1, 2, 3])
    assert fetch.started.wait(3)  # worker took [0, 1] and blocks
    loader.request([10, 11, 12])  # replaces the queued [2, 3]
    loader.request([20, 21, 1])  # replaces again; 1 is in flight: not fetched twice
    assert loader.pending() == 4  # 20, 21 queued + 0, 1 in flight
    fetch.gate.set()
    qtbot.waitUntil(lambda: loader.pending() == 0, timeout=3000)
    assert fetch.calls == [[0, 1], [20, 21]]
    assert loader.peek(0) == "v0" and loader.peek(1) == "v1"  # in-flight results are kept
    assert cached_keys(loader, [2, 3, 10, 11, 12]) == []


def test_get_misses_join_the_current_window(qtbot, make_loader):
    fetch = GatedFetch()
    loader = make_loader(fetch, batch=10)
    loader.request([0])
    assert fetch.started.wait(3)
    loader.request([5, 6])
    assert loader.get(7) is None  # appended to the window
    assert loader.get(5) is None  # already queued: no duplicate
    fetch.gate.set()
    qtbot.waitUntil(lambda: loader.pending() == 0, timeout=3000)
    assert fetch.calls == [[0], [5, 6, 7]]


def test_prefetch_has_lower_priority(qtbot, make_loader):
    fetch = GatedFetch()
    loader = make_loader(fetch, batch=2)
    loader.request([0])
    assert fetch.started.wait(3)
    loader.prefetch([1, 2])
    loader.prefetch([5, 6, 7])  # replaces the previous prefetch set
    loader.request([8, 9, 10, 6])  # 6 is promoted from prefetch to the window
    assert loader.pending() == 1 + 4 + 2
    fetch.gate.set()
    qtbot.waitUntil(lambda: loader.pending() == 0, timeout=3000)
    assert fetch.calls == [[0], [8, 9], [10, 6], [5, 7]]
    loader.prefetch([5, 8, 11])  # cached keys are skipped
    qtbot.waitUntil(lambda: loader.pending() == 0, timeout=3000)
    assert fetch.calls[-1] == [11]


def test_window_larger_than_capacity_is_truncated(qtbot, make_loader):
    fetch = GatedFetch()
    fetch.gate.set()
    loader = make_loader(fetch, capacity=5, batch=100)
    loader.request(range(20))
    qtbot.waitUntil(lambda: loader.pending() == 0, timeout=3000)
    assert fetch.calls == [[0, 1, 2, 3, 4]]


# --------------------------------------------------------------------------- cache


def test_lru_capacity(qtbot, make_loader):
    loader = make_loader(quick, capacity=3, batch=1)
    loader.request([0, 1, 2])
    qtbot.waitUntil(lambda: cached_keys(loader, range(3)) == [0, 1, 2], timeout=3000)
    assert loader.get(0) == "v0"  # touch: LRU order is now 1, 2, 0
    assert loader.peek(1) == "v1"  # peek does not touch
    loader.request([3])
    qtbot.waitUntil(lambda: loader.peek(3) is not None, timeout=3000)
    assert cached_keys(loader, range(4)) == [0, 2, 3]


def test_max_bytes_eviction(qtbot, make_loader):
    def fetch(keys, cancel):
        return {k: "x" * (100 if k == 9 else 10) for k in keys}

    loader = make_loader(fetch, capacity=100, batch=1, max_bytes=25, sizeof=len)
    loader.request([0, 1, 2])
    qtbot.waitUntil(lambda: loader.pending() == 0, timeout=3000)
    # 3 * 10 > 25, but the rows of the current window are never evicted for their size (a
    # budget smaller than the visible rows would otherwise fetch and evict them forever)
    assert cached_keys(loader, range(3)) == [0, 1, 2]
    loader.request([9])  # alone larger than max_bytes: kept (newest entry), others evicted
    qtbot.waitUntil(lambda: loader.peek(9) is not None, timeout=3000)
    assert cached_keys(loader, [0, 1, 2, 9]) == [9]
    loader.request([3])
    qtbot.waitUntil(lambda: loader.peek(3) is not None, timeout=3000)
    assert cached_keys(loader, [0, 1, 2, 3, 9]) == [3]
    assert loader._state.nbytes == 10


def test_account_grows_an_entry_and_evicts_older_ones(qtbot, make_loader):
    """A value the owner enlarged later (cells computed on the GUI thread) is accounted:
    the older entries make room, the enlarged one stays (it is touched)."""
    loader = make_loader(lambda keys, cancel: {k: "x" * 10 for k in keys}, capacity=100, batch=1,
                         max_bytes=35, sizeof=len)
    loader.request([0, 1, 2])
    qtbot.waitUntil(lambda: loader.pending() == 0, timeout=3000)
    assert cached_keys(loader, range(3)) == [0, 1, 2] and loader.cached_bytes() == 30
    loader.request([])                    # scrolled away: the entries are no longer on screen
    loader.account(0, 12)                 # 42 > 35: the oldest other entry (1) goes
    assert cached_keys(loader, range(3)) == [0, 2]
    assert loader.cached_bytes() == 32 and loader._state.sizes[0] == 22
    loader.account(5, 1000)               # not cached: nothing happens
    assert loader.cached_bytes() == 32
    loader.account(0, 100)                # alone over the budget: kept, everything else goes
    assert cached_keys(loader, range(3)) == [0] and loader.cached_bytes() == 122
    plain = make_loader(quick)            # no sizeof: a no-op
    plain.request([1])
    qtbot.waitUntil(lambda: plain.peek(1) is not None, timeout=3000)
    plain.account(1, 10)
    assert plain.cached_bytes() == 0 and plain.peek(1) == "v1"


# --------------------------------------------------------------------------- invalidate


@pytest.mark.parametrize("honor_cancel", [True, False])
def test_invalidate_discards_in_flight_results(qtbot, make_loader, honor_cancel):
    version = {"v": 1}
    fetch = GatedFetch(value=lambda k: f"{k}@{version['v']}", honor_cancel=honor_cancel)
    loader = make_loader(fetch)
    loader.request([1, 2])
    assert fetch.started.wait(3)
    version["v"] = 2
    loader.invalidate()
    assert fetch.tokens[0].cancelled  # long fetches can stop
    fetch.gate.set()
    qtbot.waitUntil(lambda: cached_keys(loader, [1, 2]) == [1, 2], timeout=3000)
    assert loader.peek(1) == "1@2" and loader.peek(2) == "2@2"  # the window is fetched again
    assert fetch.calls == [[1, 2], [1, 2]]


def test_partial_invalidate(qtbot, make_loader):
    version = {"v": 1}
    fetch = GatedFetch(value=lambda k: f"{k}@{version['v']}", honor_cancel=False)
    loader = make_loader(fetch)
    loader.request([1, 2])
    assert fetch.started.wait(3)
    version["v"] = 2
    loader.invalidate([1])
    assert not fetch.tokens[0].cancelled  # key 2 of the batch is still useful
    fetch.gate.set()
    qtbot.waitUntil(lambda: loader.peek(1) is not None, timeout=3000)
    assert loader.peek(1) == "1@2" and loader.peek(2) == "2@1"
    assert fetch.calls == [[1, 2], [1]]
    version["v"] = 3
    loader.invalidate([2, 77])  # cached and in the window: fetched again; 77 unknown
    assert loader.peek(2) is None
    qtbot.waitUntil(lambda: loader.peek(2) == "2@3", timeout=3000)
    loader.request([5])
    qtbot.waitUntil(lambda: loader.peek(5) is not None, timeout=3000)
    loader.invalidate([1])  # not in the window any more: just forgotten
    qtbot.wait(50)
    assert loader.peek(1) is None and loader.pending() == 0


def test_partial_invalidate_of_whole_batch_cancels(qtbot, make_loader):
    fetch = GatedFetch()
    loader = make_loader(fetch)
    loader.request([1, 2])
    assert fetch.started.wait(3)
    loader.invalidate([2, 1])
    assert fetch.tokens[0].cancelled
    fetch.gate.set()
    qtbot.waitUntil(lambda: cached_keys(loader, [1, 2]) == [1, 2], timeout=3000)
    assert fetch.calls == [[1, 2], [1, 2]]


# --------------------------------------------------------------------------- failures


def test_fetch_errors_become_load_failed_and_are_logged_once(qtbot, make_loader, caplog):
    def fetch(keys, cancel):
        raise ValueError("boom")

    loader = make_loader(fetch, batch=2, name="failing")
    with caplog.at_level(logging.DEBUG, logger="jsonreshape.gui.loader"):
        loader.request([1, 2, 3])
        qtbot.waitUntil(lambda: cached_keys(loader, [1, 2, 3]) == [1, 2, 3], timeout=3000)
    value = loader.get(1)
    assert isinstance(value, LoadFailed)
    assert isinstance(value.error, ValueError) and value.text == "ValueError"
    assert loader.peek(3).error is not value.error  # second batch, own error
    warnings = [r for r in caplog.records if r.name == "jsonreshape.gui.loader" and r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "failing" in warnings[0].getMessage()


def test_load_failed_text_for_user_errors(qtbot, make_loader, lang_en):
    def fetch(keys, cancel):
        raise UserInputError("gui.app.no_qt")

    loader = make_loader(fetch)
    loader.request(["k"])
    qtbot.waitUntil(lambda: loader.peek("k") is not None, timeout=3000)
    assert loader.peek("k").text == t("gui.app.no_qt")


def test_bad_fetch_result_is_a_failure(qtbot, make_loader):
    loader = make_loader(lambda keys, cancel: [1, 2])
    loader.request([1])
    qtbot.waitUntil(lambda: loader.peek(1) is not None, timeout=3000)
    assert isinstance(loader.peek(1), LoadFailed) and loader.peek(1).text == "TypeError"


# --------------------------------------------------------------------------- close


def test_close_joins_and_is_idempotent(qtbot, make_loader):
    fetch = GatedFetch()
    loader = make_loader(fetch)
    loader.request([1])
    qtbot.waitUntil(lambda: loader.peek(1) is None and fetch.started.is_set(), timeout=3000)
    fetch.gate.set()
    qtbot.waitUntil(lambda: loader.peek(1) == "v1", timeout=3000)
    fetch.gate.clear()
    fetch.started.clear()
    loader.request([2])
    assert fetch.started.wait(3)  # blocked in fetch; it honors the cancel token
    t0 = time.perf_counter()
    loader.close()
    assert time.perf_counter() - t0 < 1.5
    assert not loader._state.thread.is_alive()
    loader.close()
    emitted = []
    loader.loaded.connect(lambda: emitted.append(1))
    assert loader.get(1) == "v1"  # cached values stay readable
    assert loader.get(2) is None and loader.get(3) is None
    loader.request([4])
    loader.prefetch([5])
    loader.invalidate([1])
    assert loader.pending() == 0 and loader.peek(1) is None
    qtbot.wait(50)
    assert emitted == [] and not loader._state.thread.is_alive()


def test_close_without_worker(qapp):
    loader = RowLoader(quick)
    loader.close()
    loader.close()
    assert loader.get(1) is None and loader.pending() == 0


def test_destroying_the_loader_stops_the_worker(qtbot):
    parent = QObject()
    loader = RowLoader(quick, parent=parent)
    loader.request([1])
    qtbot.waitUntil(lambda: loader.peek(1) is not None, timeout=3000)
    thread = loader._state.thread
    del loader
    parent.deleteLater()
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=3000)

    orphan = RowLoader(quick)
    orphan.request([1])
    qtbot.waitUntil(lambda: orphan.peek(1) is not None, timeout=3000)
    thread = orphan._state.thread
    del orphan
    gc.collect()
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=3000)


# --------------------------------------------------------------------------- signals / threads


def test_loaded_is_coalesced(qtbot, make_loader):
    loader = make_loader(quick, batch=1, capacity=1000)
    emitted = []
    loader.loaded.connect(lambda: emitted.append(1))
    loader.request(range(300))
    deadline = time.monotonic() + 10
    while loader.pending() and time.monotonic() < deadline:  # no event processing meanwhile
        time.sleep(0.005)
    assert loader.pending() == 0
    assert cached_keys(loader, range(300)) == list(range(300))
    qtbot.wait(100)
    assert emitted == [1]  # 300 batches -> one queued notification
    with qtbot.waitSignal(loader.loaded, timeout=3000):
        loader.request([1000])
    assert loader.peek(1000) == "v1000"


def test_concurrent_gets_smoke(qtbot, make_loader):
    loader = make_loader(lambda keys, cancel: {k: ("v", k) for k in keys}, capacity=64, batch=8)
    errors: list[BaseException] = []

    def hammer(seed: int) -> None:
        rng = random.Random(seed)
        try:
            for _ in range(3000):
                k = rng.randrange(200)
                value = loader.get(k)
                assert value is None or value == ("v", k)
                roll = rng.random()
                if roll < 0.05:
                    loader.request(rng.sample(range(200), 10))
                elif roll < 0.06:
                    loader.invalidate([k])
                elif roll < 0.08:
                    loader.prefetch(range(k, k + 5))
                elif roll < 0.081:
                    loader.invalidate()
                loader.pending()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    while any(th.is_alive() for th in threads):
        qtbot.wait(10)
    assert not errors
    assert len(cached_keys(loader, range(200))) <= 64
    loader.request([500])
    qtbot.waitUntil(lambda: loader.peek(500) == ("v", 500), timeout=3000)
    assert loader._state.thread.is_alive()


def test_rows_of_the_window_are_not_evicted_for_size(qtbot, make_loader):
    """A byte budget smaller than the visible rows must not make the loader fetch and evict
    the rows on screen forever: window rows stay, rows outside the window make room."""
    fetched: list = []

    def fetch(keys, cancel):
        fetched.extend(keys)
        return {k: "x" * 10 for k in keys}

    loader = make_loader(fetch, capacity=100, batch=4, max_bytes=25, sizeof=len)
    loader.request(range(8))
    qtbot.waitUntil(lambda: loader.pending() == 0, timeout=3000)
    assert cached_keys(loader, range(8)) == list(range(8))
    qtbot.wait(100)
    assert sorted(fetched) == list(range(8))       # fetched once each: no fetch/evict loop
    loader.request([20])
    qtbot.waitUntil(lambda: loader.peek(20) is not None, timeout=3000)
    assert cached_keys(loader, list(range(8)) + [20]) == [7, 20]  # 20 bytes <= 25: the newest old row stays


# --------------------------------------------------------------------------- clear / failed keys (review3 H1)


def test_clear_forgets_the_window_and_fetches_nothing_again(qtbot, make_loader):
    """``clear()``: the keys mean something else now -- nothing of the old window is fetched again (``invalidate()``
    would fetch it from the new data), running results are dropped, later requests work as usual."""
    fetch = GatedFetch()
    loader = make_loader(fetch)
    loader.request([1, 2])
    assert fetch.started.wait(3)
    loader.request([1, 2, 3])                   # 3 queued behind the running batch
    loader.clear()
    assert fetch.tokens[0].cancelled
    fetch.gate.set()
    qtbot.waitUntil(lambda: loader.pending() == 0, timeout=3000)
    assert fetch.calls == [[1, 2]] and cached_keys(loader, [1, 2, 3]) == []
    loader.request([7])
    qtbot.waitUntil(lambda: loader.peek(7) == "v7", timeout=3000)
    assert fetch.calls == [[1, 2], [7]]


def test_invalidate_failed_fetches_the_failed_keys_again(qtbot, make_loader):
    failing = {1, 3}
    calls: list[list] = []

    def fetch(keys, cancel):
        calls.append(list(keys))
        if failing & set(keys):
            raise IndexError("not indexed yet")
        return {k: f"v{k}" for k in keys}

    loader = make_loader(fetch, batch=1)
    loader.request([1, 2, 3])
    qtbot.waitUntil(lambda: cached_keys(loader, [1, 2, 3]) == [1, 2, 3], timeout=3000)
    assert isinstance(loader.peek(1), LoadFailed) and loader.peek(2) == "v2"
    failing.clear()
    loader.request([1, 2])                      # 3 left the window: forgotten, not fetched
    assert loader.invalidate_failed() == 2
    qtbot.waitUntil(lambda: loader.peek(1) == "v1", timeout=3000)
    assert loader.peek(3) is None and loader.peek(2) == "v2"
    assert calls[3:] == [[1]] and loader.invalidate_failed() == 0
