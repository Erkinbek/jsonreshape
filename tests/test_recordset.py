"""RecordSet storage (docs/ARCHITECTURE.md §6; AGENT_PROMPT §3 "Yozuvlar to'plami (RecordSet)").

* builder: append/extend, live reads from another thread while the set grows (buffer +
  temporary file), memory map after ``finish()``, small sets kept in RAM;
* access: ``len``, ``[]`` with negative indices and slices, ``iter_chunks``, ``to_list``,
  ``is_ascending``;
* lifetime: ``close``/``delete``/context manager/garbage collection remove the file (also
  with chunks and views handed out on Windows), aborted and closed sets read as empty,
  :func:`purge_stale_files` removes leftovers of dead processes;
* :meth:`RecordSet.base` follows a growing index; :meth:`RecordSet.from_indices`.
"""

from __future__ import annotations

import gc
import os
import random
import subprocess
import sys
import threading
import time
from array import array
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import jsongen
from jsonreshape import recordset as rsmod
from jsonreshape.appdirs import recordsets_dir, tmp_dir
from jsonreshape.errors import JsonReshapeError
from jsonreshape.reader import Document
from jsonreshape.recordset import RecordSet, RecordSetBuilder, purge_stale_files

HYP = settings(derandomize=True, database=None, deadline=None, max_examples=60,
               suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])

BIG = 300_000  # > FLUSH_BYTES / 8: the builder writes a file, finish() memory-maps it


def rs_files() -> list[str]:
    return sorted(os.listdir(recordsets_dir()))


def f(i: int) -> int:
    """Deterministic, non-monotonic test values."""
    return (i * 2654435761) % 1_000_003


# ----------------------------------------------------------------------------------
# builder basics
# ----------------------------------------------------------------------------------
def test_small_set_stays_in_memory() -> None:
    b = RecordSetBuilder("manual", "kichik")
    for i in (5, 7, 9):
        b.append(i)
    b.extend(array("Q", [10, 20]))
    b.extend([30, 31])
    assert len(b) == 7 and b.state == "open"
    rs = b.finish()
    assert rs is b.recordset and b.state == "complete"
    assert rs.complete and not rs.closed and rs.path is None
    assert rs.to_list() == [5, 7, 9, 10, 20, 30, 31]
    assert rs.is_ascending()
    assert rs.kind == "manual" and rs.description == "kichik" and rs.report is None
    assert "manual" in repr(rs) and "7 records" in repr(rs)
    assert rs_files() == []  # nothing was ever written
    rs.close()
    assert rs.closed and len(rs) == 0


def test_large_set_is_memory_mapped_and_removed_on_close() -> None:
    b = RecordSetBuilder("filter")
    b.extend(array("Q", (f(i) for i in range(BIG // 2))))
    for i in range(BIG // 2, BIG):
        b.append(f(i))
    path = b.recordset.path
    assert path is not None and path.parent == recordsets_dir() and path.exists()  # flushed while growing
    rs = b.finish()
    assert rs.complete and rs.path == path and path.stat().st_size == 8 * BIG
    assert len(rs) == BIG
    for i in (0, 1, 12345, BIG - 1):
        assert rs[i] == f(i)
    assert rs[-1] == f(BIG - 1) and rs[-BIG] == f(0)
    assert rs[100:110].tolist() == [f(i) for i in range(100, 110)]
    assert rs[BIG - 3:BIG + 50].tolist() == [f(i) for i in range(BIG - 3, BIG)]
    assert rs[10:4:-2].tolist() == [f(i) for i in range(10, 4, -2)]
    chunks = list(rs.iter_chunks(100_000))
    assert [len(c) for c in chunks] == [100_000, 100_000, 100_000]
    assert all(isinstance(c, array) and c.typecode == "Q" for c in chunks)
    whole = array("Q")
    for c in chunks:
        whole.extend(c)
    assert whole.tolist() == [f(i) for i in range(BIG)]
    assert rs.to_list(5) == [f(i) for i in range(5)]
    assert sum(1 for _ in rs) == BIG
    assert not rs.is_ascending()
    rs.close()
    assert not path.exists() and rs_files() == []
    assert len(rs) == 0 and rs.closed
    with pytest.raises(IndexError):
        rs[0]
    rs.close()  # idempotent


def test_index_errors_and_types() -> None:
    rs = RecordSet.from_indices([3, 1, 2])
    with pytest.raises(IndexError):
        rs[3]
    with pytest.raises(IndexError):
        rs[-4]
    with pytest.raises(TypeError):
        rs["1"]  # type: ignore[index]
    assert rs[-3] == 3 and rs[True] == 1  # bool is an int index
    assert rs[:].tolist() == [3, 1, 2] and rs[5:9].tolist() == []
    rs.close()


def test_is_ascending_is_tracked() -> None:
    cases = [
        ([], True),
        ([4], True),
        ([1, 2, 3], True),
        ([1, 1, 2], False),     # strictly increasing only
        ([1, 3, 2], False),
    ]
    for values, expected in cases:
        with RecordSet.from_indices(values) as rs:
            assert rs.is_ascending() is expected, values
    b = RecordSetBuilder()
    b.extend(range(10))
    assert b.recordset.is_ascending()
    b.extend(array("Q", [10, 11, 12]))
    assert b.recordset.is_ascending()
    b.append(12)
    assert not b.recordset.is_ascending()
    b.append(50)
    assert not b.finish().is_ascending()
    b.recordset.close()
    b2 = RecordSetBuilder()
    b2.extend([5, 6, 7])
    b2.extend([8, 10, 9])  # disorder inside a batch
    assert not b2.finish().is_ascending()
    b2.recordset.close()


def test_builder_state_errors() -> None:
    b = RecordSetBuilder()
    b.append(1)
    rs = b.finish()
    with pytest.raises(ValueError):
        b.append(2)
    with pytest.raises(ValueError):
        b.finish()
    b.abort()  # a finished set is left alone
    assert rs.to_list() == [1] and not rs.closed
    rs.close()


@pytest.mark.parametrize("bad, exc", [
    ([-1], ValueError),
    ([2**64], ValueError),
    ([1.5], TypeError),
    ("123", TypeError),
    (b"\x01\x02", TypeError),
    (range(-3, 2), ValueError),
])
def test_invalid_indices(bad, exc) -> None:
    with pytest.raises(exc):
        RecordSet.from_indices(bad)
    assert rs_files() == []


def test_append_rejects_bad_values() -> None:
    b = RecordSetBuilder()
    with pytest.raises(ValueError):
        b.append(-5)
    with pytest.raises(TypeError):
        b.append(2.0)  # type: ignore[arg-type]
    b.append(2**64 - 1)
    assert b.finish().to_list() == [2**64 - 1]
    b.recordset.close()


# ----------------------------------------------------------------------------------
# live growth
# ----------------------------------------------------------------------------------
def test_live_reads_from_another_thread_while_growing() -> None:
    n_total = 120_000
    b = RecordSetBuilder("search", flush_bytes=4096)  # 512 indices per file write
    rs = b.recordset
    errors: list[str] = []
    done = threading.Event()
    seen_lengths: list[int] = []

    def reader() -> None:
        rng = random.Random(5)
        last = 0
        while not done.is_set() or last < n_total:
            n = len(rs)
            if n < last:
                errors.append(f"length went back from {last} to {n}")
                return
            last = n
            seen_lengths.append(n)
            if n:
                k = rng.randrange(n)
                if rs[k] != f(k):
                    errors.append(f"rs[{k}] = {rs[k]} != {f(k)}")
                    return
                if rs[-1] < 0:  # negative index on a growing set
                    errors.append("negative")
                lo = rng.randrange(n)
                got = rs[lo:lo + 700].tolist()
                if got != [f(i) for i in range(lo, lo + len(got))]:
                    errors.append(f"slice at {lo} differs")
                    return
            if done.is_set() and last >= n_total:
                break

    th = threading.Thread(target=reader)
    th.start()
    rng = random.Random(9)
    i = 0
    while i < n_total:
        if rng.random() < 0.3:
            b.append(f(i))
            i += 1
        else:
            k = min(n_total - i, rng.randrange(1, 3000))
            b.extend(array("Q", (f(j) for j in range(i, i + k))))
            i += k
        assert not rs.complete
    done.set()
    th.join(60)
    assert not th.is_alive() and not errors, errors
    assert len(set(seen_lengths)) > 3  # the reader really saw the set grow
    b.finish()
    assert rs.complete and len(rs) == n_total and rs.to_list() == [f(i) for i in range(n_total)]
    rs.close()
    assert rs_files() == []


def test_iter_chunks_follows_growth_and_never_waits() -> None:
    b = RecordSetBuilder(flush_bytes=64)
    b.extend(range(10))
    it = b.recordset.iter_chunks(4)
    assert next(it).tolist() == [0, 1, 2, 3]
    b.extend(range(10, 13))
    rest = [x for c in it for x in c]
    assert rest == list(range(4, 13))  # elements appended before the iterator got there
    assert list(b.recordset.iter_chunks()) == [array("Q", range(13))]
    b.abort()


def test_complete_set_read_concurrently() -> None:
    rs = RecordSet.from_indices(array("Q", (f(i) for i in range(BIG))))
    errors: list[int] = []

    def worker(seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(3000):
            k = rng.randrange(BIG)
            if rs[k] != f(k):
                errors.append(k)
        for c in rs.iter_chunks(77_777):
            if not c:
                errors.append(-1)

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    rs.close()


# ----------------------------------------------------------------------------------
# lifetime and cleanup
# ----------------------------------------------------------------------------------
def test_abort_removes_the_file_and_empties_the_set() -> None:
    b = RecordSetBuilder(flush_bytes=64)
    b.extend(range(1000))
    path = b.recordset.path
    assert path is not None and path.exists()
    b.abort()
    assert not path.exists() and b.state == "closed"
    assert len(b.recordset) == 0 and b.recordset.closed and list(b.recordset) == []
    with pytest.raises(ValueError):
        b.append(1)
    b.abort()  # idempotent


def test_context_managers() -> None:
    with RecordSet.from_indices(range(BIG)) as rs:
        path = rs.path
        assert path is not None and path.exists() and rs[-1] == BIG - 1
    assert not path.exists() and rs.closed
    with RecordSetBuilder(flush_bytes=64) as b:
        b.extend(range(100))
        tmp = b.recordset.path
    assert b.state == "closed" and tmp is not None and not tmp.exists()
    with RecordSetBuilder() as b2:
        b2.extend([1, 2])
        rs2 = b2.finish()
    assert b2.state == "complete" and rs2.to_list() == [1, 2]
    rs2.delete()
    assert rs2.closed
    assert rs_files() == []


def test_garbage_collected_set_removes_its_file() -> None:
    rs = RecordSet.from_indices(range(BIG))
    path = rs.path
    assert path is not None and path.exists()
    del rs
    gc.collect()
    assert not path.exists()
    b = RecordSetBuilder(flush_bytes=64)
    b.extend(range(500))
    tmp = b.recordset.path
    assert tmp is not None and tmp.exists()
    del b
    gc.collect()
    assert not tmp.exists()


def test_close_with_outstanding_chunks_and_iterators() -> None:
    """Windows cannot delete a mapped file: close() must release every view first, and
    data handed out (chunks, slices) are copies that outlive the set."""
    rs = RecordSet.from_indices(array("Q", (f(i) for i in range(BIG))))
    path = rs.path
    chunk = next(rs.iter_chunks(1000))
    part = rs[5000:5010]
    it = rs.iter_chunks(50_000)
    next(it)
    single = rs[77]
    rs.close()
    assert path is not None and not path.exists()
    assert chunk.tolist() == [f(i) for i in range(1000)] and part.tolist() == [f(i) for i in range(5000, 5010)]
    assert single == f(77)
    assert list(it) == []  # a suspended iteration simply ends
    assert rs.to_list() == []


def test_purge_stale_files_removes_only_dead_processes() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead = proc.pid
    me = os.getpid()
    rdir = recordsets_dir()
    stale = rdir / f"rs-{dead}-abcdef.u64"
    stale.write_bytes(b"\x00" * 16)
    mine = rdir / f"rs-{me}-012345.u64"
    mine.write_bytes(b"")
    other = rdir / "notes.txt"
    other.write_bytes(b"x")
    sdir = tmp_dir() / f"sort-{dead}-0a0b0c0d"
    sdir.mkdir()
    (sdir / "run-000001.bin").write_bytes(b"\x01\x00\x00\x00a")
    live_dir = tmp_dir() / f"sort-{me}-01020304"
    live_dir.mkdir()
    removed = purge_stale_files()
    assert removed == 2
    assert not stale.exists() and not sdir.exists()
    assert mine.exists() and other.exists() and live_dir.exists()
    for p in (mine, other):
        p.unlink()
    live_dir.rmdir()


def test_write_errors_are_localized(tmp_path: Path) -> None:
    blocker = tmp_path / "not_a_dir"
    blocker.write_bytes(b"x")
    b = RecordSetBuilder(directory=blocker, flush_bytes=64)
    with pytest.raises(JsonReshapeError):
        b.extend(range(100))
    b.abort()


def test_builder_in_custom_directory(tmp_path: Path) -> None:
    b = RecordSetBuilder(directory=tmp_path / "sets", flush_bytes=64)
    b.extend(range(BIG))
    rs = b.finish()
    assert rs.path is not None and rs.path.parent == tmp_path / "sets"
    rs.close()
    assert os.listdir(tmp_path / "sets") == []


# ----------------------------------------------------------------------------------
# from_indices / base
# ----------------------------------------------------------------------------------
def gen(n: int):
    yield from range(n - 1, -1, -1)


@pytest.mark.parametrize("make", [
    lambda: [4, 2, 9],
    lambda: (4, 2, 9),
    lambda: array("Q", [4, 2, 9]),
    lambda: array("l", [4, 2, 9]),
    lambda: iter([4, 2, 9]),
])
def test_from_indices_inputs(make) -> None:
    with RecordSet.from_indices(make(), kind="search", description="d") as rs:
        assert rs.to_list() == [4, 2, 9] and rs.complete and rs.kind == "search" and rs.description == "d"


def test_from_indices_large_inputs_and_copies() -> None:
    with RecordSet.from_indices(gen(BIG)) as rs:
        assert len(rs) == BIG and rs[0] == BIG - 1 and rs[-1] == 0 and not rs.is_ascending()
        with RecordSet.from_indices(rs) as copy:
            assert copy.path != rs.path and copy[:10].tolist() == rs[:10].tolist() and len(copy) == BIG
    with RecordSet.from_indices(range(0, 30, 3)) as r3:
        assert r3.to_list() == list(range(0, 30, 3)) and r3.is_ascending()
    with RecordSet.from_indices([]) as empty:
        assert len(empty) == 0 and empty.complete and empty.to_list() == [] and list(empty.iter_chunks()) == []
    assert rs_files() == []


def test_base_set_follows_a_growing_index(tmp_path: Path) -> None:
    recs = jsongen.records(30000, seed=4)
    p = tmp_path / "big.json"
    jsongen.write_file(p, recs, "array")
    with Document.open(p) as doc:
        base = RecordSet.base(doc)
        assert base.kind == "base" and base.description and len(base) == 0 and not base.complete
        task = doc.start_index()
        lengths = []
        while not task.done:
            lengths.append(len(base))
            time.sleep(0.0005)
        task.result()
        assert lengths == sorted(lengths)
        assert base.complete and len(base) == 30000 == doc.record_count
        assert base[0] == 0 and base[-1] == 29999 and base[123] == 123
        assert base[10:15].tolist() == [10, 11, 12, 13, 14] and base[29998:40000].tolist() == [29998, 29999]
        assert base.is_ascending() and base.path is None
        assert sum(len(c) for c in base.iter_chunks(7000)) == 30000
        assert base.to_list(3) == [0, 1, 2]
        with pytest.raises(IndexError):
            base[30000]
        base.close()
        assert len(base) == 0 and base.closed and not doc.closed


# ----------------------------------------------------------------------------------
# model-based (hypothesis)
# ----------------------------------------------------------------------------------
ops = st.lists(
    st.one_of(
        st.tuples(st.just("append"), st.integers(0, 2**64 - 1)),
        st.tuples(st.just("extend"), st.lists(st.integers(0, 10**6), max_size=40)),
    ),
    max_size=40,
)


@HYP
@given(ops=ops, flush=st.sampled_from([8, 24, 200, 1 << 20]), memory_max=st.sampled_from([0, 64, 1 << 16]))
def test_builder_matches_a_list_model(ops, flush, memory_max) -> None:
    b = RecordSetBuilder(flush_bytes=flush, memory_max=memory_max)
    model: list[int] = []
    for op, arg in ops:
        if op == "append":
            b.append(arg)
            model.append(arg)
        else:
            b.extend(arg)
            model.extend(arg)
        rs = b.recordset
        assert len(rs) == len(model)
        if model:
            assert rs[-1] == model[-1] and rs[0] == model[0]
            assert rs[len(model) // 2] == model[len(model) // 2]
    rs = b.finish()
    assert rs.to_list() == model and len(rs) == len(model)
    assert rs.is_ascending() == all(a < c for a, c in zip(model, model[1:]))
    assert [x for c in rs.iter_chunks(7) for x in c] == model
    if model:
        assert rs[-len(model)] == model[0]
    path = rs.path
    rs.close()
    assert path is None or not path.exists()


def test_module_constants_are_sane() -> None:
    assert rsmod.FLUSH_BYTES >= rsmod.MEMORY_MAX_BYTES  # a flushed set is always memory-mapped
    assert rsmod.FLUSH_BYTES <= 1 << 20                 # "<= ~1 MiB buffered"
    assert rsmod.CHUNK_ITEMS == 65536
    assert rsmod.BATCH_INTERVAL <= 0.1 + 1e-9
