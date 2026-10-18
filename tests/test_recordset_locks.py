"""RecordSet storage locking (round-2 fix R2-25): ``_Storage._lock`` is never held across
file I/O, so readers of a growing set (``rs[i]``, slices, ``iter_chunks`` -- the GUI thread
showing a filter result while it fills) never wait for a flush of the writer.

The writer's file handle is replaced by one whose ``write`` blocks until the test releases
it; meanwhile reads of every region (file, the chunk being written, the buffer) must
return at once and correctly, also with many concurrent readers; ``close()`` during a
blocked write waits for it and removes the file; ``finish()`` publishes the complete set.
"""

from __future__ import annotations

import os
import threading
import time
from array import array
from typing import Any

import pytest

from jsonreshape.recordset import RecordSetBuilder

FLUSH = 64                 # bytes: 8 indices per file write
FAST = 0.25                # a read "returns at once": well below the blocked write


class _SlowFile:
    """Wraps the writer's raw handle: ``write`` blocks until ``release`` is set (or, with
    ``delay``, sleeps that long)."""

    def __init__(self, raw: Any, delay: float | None = None) -> None:
        self.raw = raw
        self.delay = delay
        self.entered = threading.Event()
        self.release = threading.Event()
        self.writes = 0

    def write(self, data: Any) -> int:
        self.writes += 1
        self.entered.set()
        if self.delay is not None:
            time.sleep(self.delay)
        else:
            assert self.release.wait(10), "the test never released the write"
        return self.raw.write(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)


def _blocked_builder(first: int = 24, delay: float | None = None) -> tuple[RecordSetBuilder, _SlowFile]:
    """A builder whose file holds ``first`` indices (0..first-1) and whose next flushes
    block inside ``write``."""
    b = RecordSetBuilder(flush_bytes=FLUSH)
    b.extend(array("Q", range(first)))
    st = b._storage
    assert st._f is not None and st._flushed == first
    slow = _SlowFile(st._f, delay)
    st._f = slow
    return b, slow


def _timed(fn: Any) -> tuple[Any, float]:
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


def test_reads_do_not_wait_for_a_flush_in_progress() -> None:
    b, slow = _blocked_builder(24)
    rs = b.recordset
    writer = threading.Thread(target=b.extend, args=(array("Q", range(24, 32)),), daemon=True)
    writer.start()
    assert slow.entered.wait(5)           # the writer is inside the file write now
    try:
        assert len(rs) == 32
        for i, want in ((0, 0), (23, 23), (24, 24), (31, 31), (-1, 31)):
            got, took = _timed(lambda i=i: rs[i])           # file / chunk being written / last
            assert got == want and took < FAST, (i, took)
        got, took = _timed(lambda: rs[0:32])
        assert list(got) == list(range(32)) and took < FAST, took
        got, took = _timed(lambda: list(rs.iter_chunks(5)))
        assert [x for c in got for x in c] == list(range(32)) and took < FAST, took
        assert writer.is_alive() and not rs.complete
    finally:
        slow.release.set()
        writer.join(5)
    assert not writer.is_alive()
    st = b._storage
    assert st._flushed == 32 and not st._pending
    b.append(99)                           # the buffer after the written chunk
    assert rs[32] == 99 and list(rs[30:33]) == [30, 31, 99]
    out = b.finish()
    assert out.complete and list(out) == list(range(32)) + [99]
    out.close()


def test_many_readers_during_blocked_flushes() -> None:
    """Readers in several threads keep getting correct values while the writer is blocked
    in a flush, again and again."""
    b, slow = _blocked_builder(16, delay=0.05)     # every flush takes 50 ms
    rs = b.recordset
    stop = threading.Event()
    errors: list[BaseException] = []
    slowest = [0.0]

    def reader() -> None:
        try:
            while not stop.is_set():
                n = len(rs)
                if not n:
                    continue
                t0 = time.perf_counter()
                vals = rs[0:n]
                one = rs[n - 1]
                took = time.perf_counter() - t0
                slowest[0] = max(slowest[0], took)
                assert list(vals) == list(range(n)) and one == n - 1
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    for t in readers:
        t.start()

    def write_more() -> None:
        for k in range(16, 64, 8):
            b.extend(array("Q", range(k, k + 8)))

    writer = threading.Thread(target=write_more, daemon=True)
    writer.start()
    writer.join(10)
    stop.set()
    assert not writer.is_alive() and slow.writes == 6
    for t in readers:
        t.join(5)
    assert not errors, errors
    assert slowest[0] < FAST, slowest[0]
    assert len(rs) == 64 and list(rs) == list(range(64))
    b.finish().close()


def test_close_during_a_blocked_write_waits_and_removes_the_file() -> None:
    b, slow = _blocked_builder(24)
    rs = b.recordset
    path = rs.path
    assert path is not None and path.exists()
    writer = threading.Thread(target=b.extend, args=(array("Q", range(24, 32)),), daemon=True)
    writer.start()
    assert slow.entered.wait(5)
    closer = threading.Thread(target=rs.close, daemon=True)
    closer.start()
    time.sleep(0.1)
    assert closer.is_alive()               # waits for the write in progress (not for a reader)
    _, took = _timed(lambda: len(rs))
    assert took < FAST
    slow.release.set()
    writer.join(5)
    closer.join(5)
    assert not writer.is_alive() and not closer.is_alive()
    assert rs.closed and len(rs) == 0 and list(rs[0:5]) == []
    assert not os.path.exists(path)
    with pytest.raises(ValueError):
        b.append(1)


def test_finish_while_a_reader_reads_the_file() -> None:
    """``finish()`` waits only for a reader's file read in progress; readers that come
    later read the complete (memory-mapped) set."""
    b = RecordSetBuilder(flush_bytes=FLUSH)
    b.extend(array("Q", range(20_000)))
    rs = b.recordset
    st = b._storage
    assert st._flushed > 0 and not rs.complete
    got: list[Any] = []

    def reader() -> None:
        for _ in range(200):
            got.append(rs[5])
            got.append(rs[100:103])

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    out = b.finish()
    t.join(5)
    assert out.complete and out[19_999] == 19_999 and len(out) == 20_000
    assert all(x == 5 for x in got[0::2]) and all(list(x) == [100, 101, 102] for x in got[1::2])
    assert st._rf is None                  # the readers' handle is closed by finish()
    out.close()
