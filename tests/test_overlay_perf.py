"""Performance smoke tests of jsonreshape.overlay (docs/ARCHITECTURE.md §12.1, pure Python targets).

With 1,000,000 pieces: insert / delete / move of one piece < 20 ms, ``rid_at`` < 20 µs, ``rids(pos, 65536)``
built from slices (< 20 ms); deleting 1,000,000 scattered positions from a 10,000,000-record identity table
< 5 s; an identity table of any size is O(1) memory; ``pos_of`` after its one-time index; ``Remap.map_array``
merges big ascending inputs; ``OverlayState.runs`` / ``contents`` and array layers stay cheap on big data.
Timings take the best of several repetitions (robust against a busy machine).
"""

from __future__ import annotations

import random
import time
import tracemalloc
from array import array

import pytest

from jsonreshape.overlay import (
    EDIT_BASE,
    ArrayLayer,
    DictLayer,
    OverlayState,
    PieceTable,
    Remap,
    write_array_layer,
)

N_PIECES = 1_000_000


def best_of(fn, repeat: int = 5) -> float:
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


@pytest.fixture(scope="module")
def big_table() -> PieceTable:
    """1,000,000 pieces of 1..3 rids in shuffled block order (realistic after many edits)."""
    rng = random.Random(5)
    lens = [rng.randrange(1, 4) for _ in range(N_PIECES)]
    lo, start = array("Q"), 0
    for ln in lens:
        lo.append(start)
        start += ln + 1                       # gaps keep the pieces from merging
    hi = array("Q", (a + ln for a, ln in zip(lo, lens)))
    order = list(range(N_PIECES))
    for i in range(0, N_PIECES, 1000):        # shuffle inside blocks: rid order is mostly kept
        block = order[i:i + 1000]
        rng.shuffle(block)
        order[i:i + 1000] = block
    t = PieceTable.from_pairs(array("Q", (lo[i] for i in order)), array("Q", (hi[i] for i in order)))
    assert t.npieces == N_PIECES
    return t


def test_single_piece_operations_on_a_million_pieces(big_table):
    t = big_table
    mid = t.count // 2
    fresh = 10 ** 12
    assert best_of(lambda: t.insert(mid, fresh, 3)) < 0.020
    assert best_of(lambda: t.delete(mid, 2)) < 0.020
    assert best_of(lambda: t.move(array("Q", [100, 101, 102]), t.count - 500)) < 0.020
    assert best_of(lambda: t.move(array("Q", [5, 77_777, 900_000]), 12)) < 0.020
    assert best_of(lambda: t.delete_positions(array("Q", [3, 1_000, 1_500_000]))) < 0.020
    # the untouched chunks are shared, not copied
    t2 = t.insert(mid, fresh, 3)
    shared = sum(1 for a, b in zip(t._chunks, t2._chunks) if a is b)
    assert shared >= len(t._chunks) // 2 - 2
    assert t2.count == t.count + 3 and t2.rid_at(mid) == fresh


def test_reads_on_a_million_pieces(big_table):
    t = big_table
    rng = random.Random(1)
    positions = [rng.randrange(t.count) for _ in range(100_000)]
    rid_at = t.rid_at
    elapsed = best_of(lambda: [rid_at(p) for p in positions], repeat=3)
    assert elapsed / len(positions) < 20e-6
    window = best_of(lambda: t.rids(t.count // 3, 65536))
    assert window < 0.020
    assert len(t.rids(t.count // 3, 65536)) == 65536
    ident = PieceTable.identity(50_000_000)
    assert best_of(lambda: ident.rids(10_000_000, 65536)) < 0.020
    sorted_pos = array("Q", sorted(positions[:65536]))
    assert best_of(lambda: t.rids_at(sorted_pos), repeat=3) < 1.0
    # pos_of: one index build, then cheap lookups
    t0 = time.perf_counter()
    assert t.pos_of(t.rid_at(123_456)) == 123_456
    assert time.perf_counter() - t0 < 5.0
    rids = [t.rid_at(p) for p in positions[:20_000]]
    pos_of = t.pos_of
    assert best_of(lambda: [pos_of(r) for r in rids], repeat=3) / len(rids) < 20e-6


def test_identity_tables_are_constant_size():
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        tables = [PieceTable.identity(n) for n in (10, 10 ** 9, 10 ** 15, (1 << 63))]
        used = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    assert used < 8 * 4096
    assert all(t.npieces == 1 and len(t._chunks) == 1 for t in tables)
    assert tables[2].rid_at(10 ** 15 - 1) == 10 ** 15 - 1


def test_delete_a_million_scattered_positions_from_ten_million():
    ident = PieceTable.identity(10_000_000)
    rng = random.Random(2)
    positions = array("Q", sorted(rng.sample(range(10_000_000), 1_000_000)))
    t0 = time.perf_counter()
    t = ident.delete_positions(positions)
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, elapsed
    assert t.count == 9_000_000
    gone = set(positions[:2000])
    expect = [r for r in range(positions[1999] + 1) if r not in gone]
    assert t.rids(0, len(expect)).tolist() == expect
    # the Remap of that step maps a big ascending view in O(n + k)
    r = Remap([("dels", positions)])
    view = array("Q", range(0, 10_000_000, 3))
    t0 = time.perf_counter()
    out = r.map_array(view)
    assert time.perf_counter() - t0 < 5.0
    assert len(out) == len(view) - sum(1 for p in positions if p % 3 == 0)
    assert out[:5].tolist() == [p for p in (r.map(v) for v in view[:20]) if p is not None][:5]


def test_runs_and_contents_on_big_states(tmp_path):
    n = 5_000_000
    rng = random.Random(4)
    edited = sorted(rng.sample(range(n), 2000))
    top = DictLayer.from_items((r, (EDIT_BASE + 10 * r, EDIT_BASE + 10 * r + 7)) for r in edited)
    bulk_rids = range(1_000_000, 1_200_000)
    path = tmp_path / "bulk.bin"
    t0 = time.perf_counter()
    write_array_layer(path, ((r, EDIT_BASE + r, EDIT_BASE + r + 1) for r in bulk_rids), flags=0, segments=(1,),
                      work_dir=tmp_path)
    assert time.perf_counter() - t0 < 10.0
    layer = ArrayLayer.open(path)
    try:
        state = OverlayState(base_count=n, pieces=PieceTable.identity(n), layers=(layer, top))
        t0 = time.perf_counter()
        runs = list(state.runs())
        assert time.perf_counter() - t0 < 3.0
        assert sum(k for _p, _r, k, _pure in runs) == n
        impure = sum(k for _p, _r, k, pure in runs if not pure)
        assert impure == len(set(edited) | set(bulk_rids))
        assert best_of(lambda: state.contents(1_000_000, 65536), repeat=3) < 0.05   # one array-layer slice
        spans = state.contents(1_000_000, 65536)
        assert len(spans) == 2 * 65536
        assert spans[:4].tolist() == [EDIT_BASE + 1_000_000, EDIT_BASE + 1_000_001,
                                      EDIT_BASE + 1_000_001, EDIT_BASE + 1_000_002]
    finally:
        layer.close()
