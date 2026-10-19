"""jsonreshape.overlay (docs/ARCHITECTURE.md §12.1): the immutable edit state, against naive models.

* the virtual address space helpers (``EDIT_BASE``, segment/offset split, bounds);
* ``PieceTable`` vs a list of rids (hypothesis, tiny and real chunk sizes): insert / delete /
  delete_positions / move (the "``to`` without the moved records" semantics) / apply(remap) incl. new_rids,
  rid_at / rids / rids_at (sorted, unsorted, duplicates) / pos_of / runs, canonical form (contiguous pieces
  merged also across chunks), chunk-size invariants, to_bytes / from_bytes, equality, persistence (older
  versions never change), validation errors;
* ``Remap`` vs list simulation for random op sequences of every kind (ins, del, inss, dels, move, unmove,
  pieces): map, map_array (ascending, arbitrary order, duplicates, both merge and bisect paths),
  map_chunks, inverse (exact), then (composition), delta, to_json / from_json inline and through blobs;
* ``DictLayer`` vs a dict (persistence, keys_in over narrow and wide ranges, spans_in, items, bytes);
* ``OverlayState.runs`` / ``contents`` vs a per-record classification with several dict and array layers,
  tiny windows and cluster gaps; generations (thread-safe, unique, derive / replace rules); ChangeSet and
  NodeChange.
"""

from __future__ import annotations

import dataclasses
import json
import random
import threading
from array import array
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from jsonreshape import overlay as ov
from jsonreshape.errors import EditError
from jsonreshape.overlay import (
    EDIT_BASE,
    MAX_SEGMENT,
    SEG_SHIFT,
    SEG_SIZE_LIMIT,
    ArrayLayer,
    ChangeSet,
    DatReader,
    DictLayer,
    NodeChange,
    OverlayState,
    PieceTable,
    Remap,
    edit_offset,
    invert_node_changes,
    is_edit_offset,
    next_gen,
    segment_name,
    split_edit_offset,
    write_array_layer,
)

HYP = settings(derandomize=True, database=None, deadline=None, max_examples=150,
               suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
U64_MAX = (1 << 64) - 1


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
@pytest.fixture(params=[(4, 2), (8, 3), (4096, 1024)], ids=["chunk4", "chunk8", "chunk4096"])
def chunk_sizes(request, monkeypatch):
    """Run a test with tiny chunks (many chunks, balancing, cross-chunk merges) and the real size."""
    mx, mn = request.param
    monkeypatch.setattr(ov, "CHUNK_MAX", mx)
    monkeypatch.setattr(ov, "CHUNK_MIN", mn)
    return mx, mn


def table_of(rids: list[int]) -> PieceTable:
    return PieceTable.from_pairs(array("Q", rids), array("Q", [r + 1 for r in rids]))


def as_list(t: PieceTable) -> list[int]:
    return t.rids(0, t.count).tolist()


def n_runs(model: list[int]) -> int:
    return 0 if not model else 1 + sum(1 for a, b in zip(model, model[1:]) if b != a + 1)


def check_table(t: PieceTable, model: list[int]) -> None:
    """Every read API of ``t`` against the list ``model`` plus the structural invariants."""
    assert t.count == len(model)
    assert as_list(t) == model
    assert t.npieces == n_runs(model)            # canonical: maximal pieces only
    chunks = t._chunks
    for ch in chunks:
        assert 1 <= len(ch.lo) <= ov.CHUNK_MAX
        if len(chunks) > 1:
            assert len(ch.lo) >= ov.CHUNK_MIN
        assert ch.count == sum(h - lo for lo, h in zip(ch.lo, ch.hi))
    lo, hi = t._flat()
    assert all(h != nxt for h, nxt in zip(hi, lo[1:]))
    for i, r in enumerate(model):
        assert t.rid_at(i) == r
        assert t.pos_of(r) == i
    absent = (max(model) + 1) if model else 0
    assert t.pos_of(absent) is None
    with pytest.raises(IndexError):
        t.rid_at(len(model))
    with pytest.raises(IndexError):
        t.rid_at(-1)
    flat: list[int] = []
    for p, r, n in t.runs():
        assert p == len(flat) and n > 0
        flat.extend(range(r, r + n))
    assert flat == model
    assert PieceTable.from_bytes(t.to_bytes()) == t
    assert table_of(model) == t


@st.composite
def rid_lists(draw, max_blocks: int = 8, max_len: int = 6) -> list[int]:
    """Unique rids made of consecutive blocks with gaps, in shuffled block order (realistic pieces)."""
    base = draw(st.sampled_from([0, 0, 0, 1000, 2 ** 63, U64_MAX - 10_000]))
    blocks = draw(st.lists(st.integers(1, max_len), max_size=max_blocks))
    gaps = draw(st.lists(st.integers(0, 3), min_size=len(blocks), max_size=len(blocks)))
    pieces, start = [], base
    for ln, gap in zip(blocks, gaps):
        start += gap
        pieces.append(list(range(start, start + ln)))
        start += ln
    order = draw(st.permutations(range(len(pieces))))
    return [r for i in order for r in pieces[i]]


def draw_op(data, cur: list[int], nxt: int):
    """A random structural op on the list ``cur``: (remap op, new list, new_rids entries, next fresh rid)."""
    n = len(cur)
    kinds = ["ins", "inss"] + (["del", "dels", "move", "unmove", "pieces"] if n else [])
    kind = data.draw(st.sampled_from(kinds))
    if kind == "ins":
        pos = data.draw(st.integers(0, n))
        k = data.draw(st.integers(1, 4))
        entries, rids = [], []
        covered = 0
        while covered < k:
            ln = data.draw(st.integers(1, k - covered))
            entries.append((pos + covered, nxt, ln))
            rids.extend(range(nxt, nxt + ln))
            nxt += ln + data.draw(st.integers(0, 2))
            covered += ln
        return ("ins", pos, k), cur[:pos] + rids + cur[pos:], entries, nxt
    if kind == "del":
        pos = data.draw(st.integers(0, n - 1))
        k = data.draw(st.integers(1, n - pos))
        return ("del", pos, k), cur[:pos] + cur[pos + k:], [], nxt
    if kind == "inss":
        k = data.draw(st.integers(1, 5))
        P = sorted(data.draw(st.sets(st.integers(0, n + k - 1), min_size=k, max_size=k)))
        entries, rids = [], []
        covered = 0
        while covered < k:
            ln = data.draw(st.integers(1, k - covered))
            entries.append((P[covered], nxt, ln))
            rids.extend(range(nxt, nxt + ln))
            nxt += ln + data.draw(st.integers(0, 2))
            covered += ln
        new: list = [None] * (n + k)
        for j, p in enumerate(P):
            new[p] = rids[j]
        it = iter(cur)
        return ("inss", P), [x if x is not None else next(it) for x in new], entries, nxt
    if kind == "dels":
        D = sorted(data.draw(st.sets(st.integers(0, n - 1), min_size=1, max_size=n)))
        gone = set(D)
        return ("dels", D), [x for i, x in enumerate(cur) if i not in gone], [], nxt
    if kind == "move":
        P = sorted(data.draw(st.sets(st.integers(0, n - 1), min_size=1, max_size=n)))
        to = data.draw(st.integers(0, n - len(P)))
        gone = set(P)
        rest = [x for i, x in enumerate(cur) if i not in gone]
        rest[to:to] = [cur[i] for i in P]
        return ("move", P, to), rest, [], nxt
    if kind == "unmove":
        k = data.draw(st.integers(1, n))
        to = data.draw(st.integers(0, n - k))
        P = sorted(data.draw(st.sets(st.integers(0, n - 1), min_size=k, max_size=k)))
        moved, rest = cur[to:to + k], cur[:to] + cur[to + k:]
        new = [None] * n
        for j, p in enumerate(P):
            new[p] = moved[j]
        it = iter(rest)
        return ("unmove", to, P), [x if x is not None else next(it) for x in new], [], nxt
    # pieces: an arbitrary reorder / subset / additions (reset-free big patch)
    keep = [x for x in cur if data.draw(st.booleans()) or data.draw(st.booleans())]
    keep = data.draw(st.permutations(keep)) if data.draw(st.booleans()) else keep
    keep = list(keep)
    for _ in range(data.draw(st.integers(0, 2))):
        keep.insert(data.draw(st.integers(0, len(keep))), nxt)
        nxt += 1
    return ("pieces", table_of(cur), table_of(keep)), keep, [], nxt


# ----------------------------------------------------------------------------------
# virtual address space
# ----------------------------------------------------------------------------------
def test_edit_offsets_round_trip_and_bounds():
    assert EDIT_BASE == 1 << 48 and SEG_SHIFT == 40
    assert not is_edit_offset(EDIT_BASE - 1) and is_edit_offset(EDIT_BASE)
    for seg, off in [(0, 0), (0, 1), (3, 12345), (1, SEG_SIZE_LIMIT - 1), (MAX_SEGMENT, SEG_SIZE_LIMIT - 1)]:
        v = edit_offset(seg, off)
        assert is_edit_offset(v)
        assert v <= U64_MAX
        assert split_edit_offset(v) == (seg, off)
    assert edit_offset(0, 0) == EDIT_BASE
    assert edit_offset(2, 5) == EDIT_BASE + (2 << 40) + 5
    assert edit_offset(MAX_SEGMENT, SEG_SIZE_LIMIT - 1) == U64_MAX
    for bad in [(-1, 0), (0, -1), (0, SEG_SIZE_LIMIT), (MAX_SEGMENT + 1, 0)]:
        with pytest.raises(ValueError):
            edit_offset(*bad)
    with pytest.raises(ValueError):
        split_edit_offset(EDIT_BASE - 1)
    with pytest.raises(ValueError):
        split_edit_offset(U64_MAX + 1)
    with pytest.raises(TypeError):
        edit_offset(1.5, 0)
    assert segment_name(0) == "seg-00000.dat" and segment_name(123) == "seg-00123.dat"
    assert segment_name(123456) == "seg-123456.dat"


# ----------------------------------------------------------------------------------
# PieceTable
# ----------------------------------------------------------------------------------
def test_identity_tables():
    for n in (0, 1, 5, 10 ** 15):
        t = PieceTable.identity(n)
        assert t.count == n and t.npieces == (1 if n else 0)
        assert len(t._chunks) == (1 if n else 0)
    t = PieceTable.identity(10 ** 15)
    assert t.rid_at(10 ** 15 - 1) == 10 ** 15 - 1
    assert t.pos_of(123456789012) == 123456789012 and t.pos_of(10 ** 15) is None
    assert t.rids(10 ** 15 - 3, 10).tolist() == [10 ** 15 - 3, 10 ** 15 - 2, 10 ** 15 - 1]
    assert t.rids_at(array("Q", [5, 2, 5])).tolist() == [5, 2, 5]
    assert list(t.runs(7, 9)) == [(7, 7, 2)]
    assert PieceTable.identity(0) == PieceTable.from_pairs([], [])
    assert PieceTable.identity(3) == table_of([0, 1, 2])
    assert PieceTable.identity(3) != PieceTable.identity(4)
    assert PieceTable.identity(3) != table_of([0, 2, 1])
    assert (PieceTable.identity(3) == 3) is False
    with pytest.raises(ValueError):
        PieceTable.identity(-1)
    with pytest.raises(TypeError):
        hash(PieceTable.identity(1))


def test_from_pairs_normalizes_and_validates(chunk_sizes):
    # [5,7) [7,9) [3,3) [9,12) [0,3): the empty piece is dropped, then [5,7)+[7,9)+[9,12) merge
    t = PieceTable.from_pairs([5, 7, 3, 9, 0], [7, 9, 3, 12, 3])
    assert t.npieces == 2 and as_list(t) == [5, 6, 7, 8, 9, 10, 11, 0, 1, 2]
    t = PieceTable.from_pairs([5, 0, 3, 7, 9], [7, 3, 3, 9, 12])
    assert t.npieces == 3 and as_list(t) == [5, 6, 0, 1, 2, 7, 8, 9, 10, 11]
    with pytest.raises(ValueError):
        PieceTable.from_pairs([5], [4])          # lo > hi
    with pytest.raises(ValueError):
        PieceTable.from_pairs([0, 2], [3, 4])    # rid 2 twice
    with pytest.raises(ValueError):
        PieceTable.from_pairs([10, 0], [12, 11])  # overlap, unsorted
    with pytest.raises(ValueError):
        PieceTable.from_pairs([0, 1], [1])       # length mismatch
    with pytest.raises(ValueError):
        PieceTable.from_pairs([-1], [1])
    with pytest.raises(ValueError):
        PieceTable.from_bytes(b"\x00" * 15)
    with pytest.raises(ValueError):            # an empty piece is invalid in a blob
        PieceTable.from_bytes(array("Q", [3, 3]).tobytes())
    with pytest.raises(ValueError):            # duplicated rid in a blob
        PieceTable.from_bytes(array("Q", [0, 5, 4, 6]).tobytes())
    assert PieceTable.from_bytes(b"") == PieceTable.identity(0)


@HYP
@given(data=st.data())
def test_piece_table_matches_list_model(chunk_sizes, data):
    model = data.draw(rid_lists())
    t = table_of(model)
    check_table(t, model)
    versions = [(t, list(model))]
    nxt = (max(model) + 1 if model else 0) + 3
    taken = set(model)
    for _ in range(data.draw(st.integers(1, 10))):
        n = len(model)
        kind = data.draw(st.sampled_from(["insert", "insert", "delete", "delete_positions", "move", "remap"]))
        if kind == "insert":
            pos = data.draw(st.integers(0, n))
            k = data.draw(st.integers(1, 4))
            rid_lo = nxt
            # sometimes continue the rid run of the left neighbour: the pieces must merge
            if pos and data.draw(st.booleans()):
                cand = model[pos - 1] + 1
                if cand + k <= U64_MAX and not taken.intersection(range(cand, cand + k)) and cand + k <= nxt:
                    rid_lo = cand
            t = t.insert(pos, rid_lo, k)
            model[pos:pos] = range(rid_lo, rid_lo + k)
            taken.update(range(rid_lo, rid_lo + k))
            if rid_lo == nxt:
                nxt += k + data.draw(st.integers(0, 2))
        elif kind == "delete":
            pos = data.draw(st.integers(0, n))
            k = data.draw(st.integers(0, n - pos))
            t = t.delete(pos, k)
            del model[pos:pos + k]
        elif kind == "delete_positions":
            D = sorted(data.draw(st.sets(st.integers(0, max(0, n - 1)), max_size=n))) if n else []
            gone = set(D)
            t = t.delete_positions(array("Q", D))
            model = [x for i, x in enumerate(model) if i not in gone]
        elif kind == "move":
            if not n:
                continue
            P = sorted(data.draw(st.sets(st.integers(0, n - 1), min_size=1, max_size=n)))
            to = data.draw(st.integers(0, n - len(P)))
            gone = set(P)
            rest = [x for i, x in enumerate(model) if i not in gone]
            rest[to:to] = [model[i] for i in P]
            t = t.move(array("Q", P), to)
            model = rest
        else:
            op, model, entries, nxt = draw_op(data, model, nxt)
            t = t.apply(Remap([op]), new_rids=entries)
        taken.update(model)
        assert as_list(t) == model
        versions.append((t, list(model)))
    check_table(t, model)
    for tv, mv in versions:                       # persistence: older tables never change
        assert as_list(tv) == mv


@HYP
@given(model=rid_lists(max_blocks=10), data=st.data())
def test_piece_table_reads(chunk_sizes, model, data):
    t = table_of(model)
    n = len(model)
    for _ in range(5):
        pos = data.draw(st.integers(-2, n + 2))
        k = data.draw(st.integers(-1, n + 3))
        assert t.rids(pos, k).tolist() == [model[i] for i in range(pos, pos + k) if 0 <= i < n]
        a = data.draw(st.integers(0, n))
        b = data.draw(st.integers(a, n))
        segs = list(t.runs(a, b))
        flat = [r for _p, r0, m in segs for r in range(r0, r0 + m)]
        assert flat == model[a:b]
        assert all(p == a + sum(m for _q, _r, m in segs[:i]) for i, (p, _r, _m) in enumerate(segs))
    if n:
        sorted_pos = sorted(data.draw(st.lists(st.integers(0, n - 1), max_size=30)))
        assert t.rids_at(array("Q", sorted_pos)).tolist() == [model[p] for p in sorted_pos]
        any_pos = data.draw(st.lists(st.integers(0, n - 1), max_size=30))
        assert t.rids_at(any_pos).tolist() == [model[p] for p in any_pos]
        with pytest.raises(IndexError):
            t.rids_at([n])
    assert t.rids_at([]).tolist() == []


def test_rids_clipping():
    t = table_of([5, 6, 7, 1, 2, 9])
    assert t.rids(0, 100).tolist() == [5, 6, 7, 1, 2, 9]
    assert t.rids(4, 100).tolist() == [2, 9]
    assert t.rids(6, 1).tolist() == [] and t.rids(10, 5).tolist() == []
    assert t.rids(2, 0).tolist() == [] and t.rids(2, -3).tolist() == []
    assert t.rids(-5, 7).tolist() == [5, 6]       # negative start clipped to 0
    assert list(t.runs(1, 5)) == [(1, 6, 2), (3, 1, 2)]
    assert list(t.runs(5, 5)) == [] and list(t.runs(9, 12)) == []


def test_move_semantics_examples():
    t = PieceTable.identity(6)                      # A B C D E F = 0 1 2 3 4 5
    # take B, D out (-> A C E F) and re-insert them so that B lands at position 3 of the table without them
    assert as_list(t.move([1, 3], 3)) == [0, 2, 4, 1, 3, 5]
    assert as_list(t.move([1, 3], 0)) == [1, 3, 0, 2, 4, 5]
    assert as_list(t.move([1, 3], 4)) == [0, 2, 4, 5, 1, 3]
    assert t.move([2, 3], 2) is t                                   # a block onto itself
    assert as_list(t.move([0, 1, 2, 3, 4, 5], 0)) == [0, 1, 2, 3, 4, 5]
    # "drop before row r" (pre-step coordinates) -> to = Remap(("dels", positions)).map(r)
    positions, r = [1, 3], 5                       # drop B, D before F
    to = Remap([("dels", positions)]).map(r)
    assert as_list(t.move(positions, to)) == [0, 2, 4, 1, 3, 5]
    with pytest.raises(IndexError):
        t.move([1, 3], 5)                          # to > count - len(positions)
    with pytest.raises(IndexError):
        t.move([6], 0)
    with pytest.raises(ValueError):
        t.move([3, 1], 0)
    with pytest.raises(ValueError):
        t.move([1, 1], 0)
    assert t.move([], 6) is t
    with pytest.raises(IndexError):
        t.move([], 7)


def test_structural_errors():
    t = table_of([0, 1, 2, 10, 11])
    with pytest.raises(IndexError):
        t.insert(6, 100, 1)
    with pytest.raises(IndexError):
        t.insert(-1, 100, 1)
    with pytest.raises(ValueError):
        t.insert(0, -1, 1)
    with pytest.raises(ValueError):
        t.insert(0, U64_MAX, 1)
    assert t.insert(2, 100, 0) is t
    with pytest.raises(IndexError):
        t.delete(3, 3)
    with pytest.raises(IndexError):
        t.delete(-1, 1)
    assert t.delete(5, 0) is t
    with pytest.raises(ValueError):
        t.delete_positions([2, 1])
    with pytest.raises(ValueError):
        t.delete_positions([1, 1])
    with pytest.raises(IndexError):
        t.delete_positions([1, 5])
    assert t.delete_positions([]) is t
    assert as_list(t.delete_positions([0, 1, 2, 3, 4])) == []
    assert as_list(t.delete_positions([1, 2, 3])) == [0, 11]
    # apply: new_rids must cover the inserted items exactly
    with pytest.raises(ValueError):
        t.apply(Remap([("ins", 1, 2)]))
    with pytest.raises(ValueError):
        t.apply(Remap([("ins", 1, 2)]), new_rids=[(0, 100, 2)])        # wrong position
    with pytest.raises(ValueError):
        t.apply(Remap([("ins", 1, 2)]), new_rids=[(1, 100, 3)])        # covers too much
    with pytest.raises(ValueError):
        t.apply(Remap([("ins", 1, 2)]), new_rids=[(1, 100, 2), (3, 200, 1)])   # left over
    with pytest.raises(ValueError):
        t.apply(Remap([("ins", 1, 1)]), new_rids=[(1, 100)])
    with pytest.raises(ValueError):
        t.apply(Remap([("inss", [0, 3])]), new_rids=[(0, 100, 1), (2, 101, 1)])   # 2nd item is at 3
    assert as_list(t.apply(Remap([("inss", [0, 3])]), new_rids=[(0, 100, 1), (3, 300, 1)])) == \
        [100, 0, 1, 300, 2, 10, 11]
    assert as_list(t.apply(Remap([("inss", [0, 3])]), new_rids=[(0, 100, 2)])) == [100, 0, 1, 101, 2, 10, 11]
    with pytest.raises(ValueError):           # a 'pieces' op must start from this table
        t.apply(Remap([("pieces", PieceTable.identity(5), PieceTable.identity(4))]))
    other = table_of([11, 0])
    assert t.apply(Remap([("pieces", table_of([0, 1, 2, 10, 11]), other)])) is other


def test_rarely_used_paths(chunk_sizes):
    t = table_of([4, 5, 0, 9])
    assert t == t and "PieceTable(count=4" in repr(t)
    with pytest.raises(ValueError):
        t.rids_at([-1])
    with pytest.raises(ValueError):
        PieceTable.identity(U64_MAX + 1)
    assert PieceTable.from_pairs([3, 7], [3, 7]) == PieceTable.identity(0)      # only empty pieces
    assert ov._consecutive_pieces(array("Q")) == (array("Q"), array("Q"))
    ps, ls, ns = t._segments(-3, 10 ** 9)                                       # clipped window
    assert (ps.tolist(), ls.tolist(), ns.tolist()) == ([0, 2, 3], [4, 0, 9], [2, 1, 1])
    assert t._segments(3, 1) == (array("Q"), array("Q"), array("Q"))
    # an op that inserts an existing rid is a caller bug; the rid index notices it
    bad = t.insert(0, 9, 1)
    with pytest.raises(ValueError):
        bad.pos_of(9)
    # pieces remap into an empty table: every position is dropped
    r = Remap([("pieces", t, PieceTable.identity(0))])
    assert r.map_array([0, 3, 1]).tolist() == [] and r.map(2) is None
    assert PieceTable.identity(0)._pos_of_array(array("Q")) == (array("Q"), b"")
    # apply() refuses ops that do not fit the table
    with pytest.raises(IndexError):
        t.apply(Remap([("ins", 9, 1)]), new_rids=[(9, 100, 1)])
    with pytest.raises(IndexError):
        t.apply(Remap([("unmove", 3, [0, 1])]))
    with pytest.raises(IndexError):
        t.apply(Remap([("unmove", 0, [0, 7])]))
    with pytest.raises(IndexError):
        t.apply(Remap([("inss", [1, 9])]), new_rids=[(1, 100, 2)])
    with pytest.raises(ValueError):
        t.apply(Remap([("ins", 1, 1)]), new_rids=[(1, 100, 0)])
    # private guards (unreachable through validated Remaps)
    assert t._unmove(0, array("Q")) is t
    with pytest.raises(ValueError):
        t._unmove(0, array("Q", [2, 1]))
    assert t._insert_at(array("Q"), array("Q"), array("Q")) is t
    with pytest.raises(ValueError):
        t._insert_at(array("Q"), array("Q", [50]), array("Q", [51]))
    with pytest.raises(ValueError):
        t._insert_at(array("Q", [2, 0]), array("Q", [50]), array("Q", [52]))
    with pytest.raises(ValueError):
        t._insert_at(array("Q", [0, 2]), array("Q", [50]), array("Q", [51]))   # 2 positions, 1 rid
    for bad_op in [("inss", [1], 2), ("dels",), ("move", [1]), ("unmove", 1)]:
        with pytest.raises(ValueError):
            Remap([bad_op])
    # runs() without overriding layers: whole pieces of new rids are impure
    s = OverlayState(base_count=2, pieces=table_of([5, 6, 0, 1]), layers=(DictLayer(),))
    assert list(s.runs()) == [(0, 5, 2, False), (2, 0, 2, True)]


def test_array_layer_close_gives_up_on_a_held_slice(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(ov, "CLOSE_WAIT", 0.05)
    layer = make_array_layer(tmp_path, {1: (2, 3)})
    view = layer._raw[0:8]                       # a reader that never releases its slice
    layer.close()                                # returns (logs) instead of hanging or raising
    assert layer.closed and "still in use" in caplog.text
    view.release()


def test_big_rids_and_contiguity_across_operations():
    big = U64_MAX - 5
    t = PieceTable.from_pairs([big], [U64_MAX])     # rids big .. U64_MAX-1
    assert t.count == 5 and t.rid_at(4) == U64_MAX - 1
    t2 = t.delete_positions([1, 3])
    assert as_list(t2) == [big, big + 2, big + 4]
    t3 = t2.insert(1, big + 1, 1).insert(3, big + 3, 1)
    assert t3.npieces == 1 and t3 == t                  # re-inserting the deleted rids merges back
    assert t3.pos_of(U64_MAX - 1) == 4 and t3.pos_of(U64_MAX) is None


# ----------------------------------------------------------------------------------
# Remap
# ----------------------------------------------------------------------------------
def remap_case(data, n_max: int = 25):
    n0 = data.draw(st.integers(0, n_max))
    orig = list(range(n0))
    cur, nxt = list(orig), 1000
    ops, entries = [], []
    for _ in range(data.draw(st.integers(0, 5))):
        op, cur, e, nxt = draw_op(data, cur, nxt)
        ops.append(op)
        entries.extend(e)
    return orig, cur, ops, entries, nxt


@HYP
@given(data=st.data())
def test_remap_matches_list_simulation(data):
    orig, final, ops, entries, _nxt = remap_case(data)
    n0 = len(orig)
    r = Remap(ops)
    where = {x: i for i, x in enumerate(final)}
    back = {x: i for i, x in enumerate(orig)}
    expected = [where.get(x) for x in orig]
    assert [r.map(p) for p in range(n0)] == expected
    assert r.delta() == len(final) - n0
    assert r.identity == (not r.ops)
    # map_array: ascending (all / subset), arbitrary order with duplicates
    assert r.map_array(array("Q", range(n0))).tolist() == [e for e in expected if e is not None]
    subset = sorted(data.draw(st.sets(st.integers(0, max(0, n0 - 1)), max_size=n0))) if n0 else []
    assert r.map_array(subset).tolist() == [expected[p] for p in subset if expected[p] is not None]
    arb = data.draw(st.lists(st.integers(0, max(0, n0 - 1)), max_size=30)) if n0 else []
    assert r.map_array(arb).tolist() == [expected[p] for p in arb if expected[p] is not None]
    # map_chunks streams the same result
    size = data.draw(st.integers(1, 7))
    chunks = [array("Q", range(a, min(n0, a + size))) for a in range(0, n0, size)]
    assert [v for c in r.map_chunks(chunks) for v in c] == [e for e in expected if e is not None]
    # inverse is exact: new items map to None
    inv = r.inverse()
    assert [inv.map(q) for q in range(len(final))] == [back.get(x) for x in final]
    assert inv.map_array(array("Q", range(len(final)))).tolist() == \
        [back[x] for x in final if x in back]
    assert inv.delta() == -r.delta()
    inv2 = inv.inverse()
    assert [inv2.map(p) for p in range(n0)] == expected
    # applying the ops to a piece table gives the simulated list
    assert as_list(table_of(orig).apply(r, new_rids=entries)) == final


@HYP
@given(data=st.data())
def test_remap_then_composes(data):
    orig, mid, ops1, _e1, nxt = remap_case(data)
    cur, ops2 = list(mid), []
    for _ in range(data.draw(st.integers(0, 4))):
        op, cur, _e, nxt = draw_op(data, cur, nxt)
        ops2.append(op)
    r1, r2 = Remap(ops1), Remap(ops2)
    both = r1.then(r2)
    where = {x: i for i, x in enumerate(cur)}
    assert [both.map(p) for p in range(len(orig))] == [where.get(x) for x in orig]
    assert both.delta() == r1.delta() + r2.delta()
    assert both.ops == r1.ops + r2.ops
    with pytest.raises(TypeError):
        r1.then([])  # type: ignore[arg-type]


@HYP
@given(data=st.data())
def test_remap_json_round_trip(monkeypatch, data):
    monkeypatch.setattr(ov, "BLOB_MIN", data.draw(st.sampled_from([0, 2, 4096])))
    orig, final, ops, _entries, _nxt = remap_case(data)
    r = Remap(ops)
    store: dict[tuple[int, int], bytes] = {}

    def writer(b: bytes) -> list[int]:
        span = (EDIT_BASE + 1000 * len(store), EDIT_BASE + 1000 * len(store) + len(b))
        store[span] = b
        return list(span)

    for blob_writer in (None, writer):
        doc = json.loads(json.dumps(r.to_json(blob_writer)))
        r2 = Remap.from_json(doc, (lambda a, b: store[(a, b)]) if blob_writer else None)
        assert [r2.map(p) for p in range(len(orig))] == [r.map(p) for p in range(len(orig))]
        assert r2.delta() == r.delta()
    if store and ov.BLOB_MIN < 2:
        with pytest.raises(ValueError):            # blobs need a reader
            Remap.from_json(json.loads(json.dumps(r.to_json(writer))))


def test_remap_json_shapes_and_errors(monkeypatch):
    r = Remap([("ins", 3, 2), ("dels", [1, 4]), ("move", [0, 2], 1), ("unmove", 1, [0, 2]),
               ("del", 0, 1), ("inss", [5])])
    assert r.to_json() == [["ins", 3, 2], ["dels", [1, 4]], ["move", [0, 2], 1], ["unmove", 1, [0, 2]],
                           ["del", 0, 1], ["inss", [5]]]
    monkeypatch.setattr(ov, "BLOB_MIN", 1)
    calls = []
    out = r.to_json(lambda b: calls.append(b) or [EDIT_BASE, EDIT_BASE + len(b)])
    assert out[1] == ["dels", {"blob": [EDIT_BASE, EDIT_BASE + 16]}]
    assert calls[0] == array("Q", [1, 4]).tobytes()
    assert out[0] == ["ins", 3, 2]               # scalars stay inline
    pieces = Remap([("pieces", table_of([0, 1, 2]), table_of([2, 0]))]).to_json()
    assert pieces == [["pieces", [0, 3], [2, 3, 0, 1]]]
    for bad in ["x", [["nope", 1, 2]], [["ins", 1]], [["ins", -1, 2]], [["ins", 1.5, 2]], [["dels", [2, 1]]],
                [["dels", "12"]], [["dels", [True]]], [["dels", {"blob": [1]}]], [["pieces", [0, 1, 2], [0, 1]]],
                [["move", [1], -1]], [[]], [["dels", {"blob": [1, 2], "x": 1}]]]:
        with pytest.raises(ValueError):
            Remap.from_json(bad, lambda a, b: b"")
    with pytest.raises(ValueError):              # blob bytes not a multiple of 8
        Remap.from_json([["dels", {"blob": [1, 4]}]], lambda a, b: b"\x01\x02\x03")


def test_remap_normalizes_and_validates_ops():
    t = PieceTable.identity(3)
    r = Remap([("ins", 2, 0), ("del", 1, 0), ("inss", []), ("dels", ()), ("move", [], 0), ("move", [1, 2], 1),
               ("unmove", 4, [4, 5]), ("pieces", t, t)])
    assert r.identity and r.ops == () and r.map(7) == 7
    assert r.map_array([3, 1]).tolist() == [3, 1]
    assert r.delta() == 0 and r.inverse().identity
    for bad in [("ins", 1), ("ins", -1, 1), ("bogus", 1, 1), ("dels", [3, 3]), ("dels", [3, 1]), "ins",
                ("move", [1], -2), ("pieces", t, None), ("inss", [-1]), (), ("unmove", [1], 2)]:
        with pytest.raises((ValueError, TypeError)):
            Remap([bad])
    src = array("Q", [1, 5])
    r = Remap([("dels", src)])
    src[0] = 4                                   # the remap keeps its own copy
    assert r.map(1) is None and r.map(4) == 3
    with pytest.raises(ValueError):
        r.map(-1)
    with pytest.raises(IndexError):
        Remap([("pieces", PieceTable.identity(2), PieceTable.identity(3))]).map(2)
    with pytest.raises(IndexError):
        Remap([("pieces", PieceTable.identity(2), PieceTable.identity(3))]).map_array([0, 2])
    assert "dels" in repr(Remap([("dels", [1])]))


@pytest.mark.parametrize("dense", [False, True])
def test_map_array_merge_and_bisect_paths_agree(dense):
    rng = random.Random(11 + dense)
    n = 20_000
    k = 15_000 if dense else 40
    D = sorted(rng.sample(range(n), k))
    P = sorted(rng.sample(range(n - k + k), k))
    G = sorted(rng.sample(range(n + k), k))
    for ops in ([("dels", D)], [("inss", G)], [("del", 100, 50), ("ins", 7, 3)], [("move", P, 5)],
                [("unmove", 3, P)], [("dels", D), ("inss", G), ("move", P, 0)]):
        r = Remap(ops)
        xs = sorted(rng.sample(range(n), 5000))
        expect = [r.map(p) for p in xs]
        assert r.map_array(array("Q", xs)).tolist() == [e for e in expect if e is not None]
        shuffled = list(xs)
        rng.shuffle(shuffled)
        exp2 = [r.map(p) for p in shuffled]
        assert r.map_array(shuffled).tolist() == [e for e in exp2 if e is not None]


def test_map_chunks_checks_cancel():
    from jsonreshape.errors import Cancelled
    from jsonreshape.tasks import CancelToken

    tok = CancelToken()
    r = Remap([("dels", [0, 1])])
    gen = r.map_chunks([array("Q", [0, 1]), array("Q", [2, 3]), array("Q", [4])], cancel=tok)
    assert next(gen).tolist() == [0, 1]           # the first chunk maps to nothing and is skipped
    tok.cancel()
    with pytest.raises(Cancelled):
        next(gen)


# ----------------------------------------------------------------------------------
# NodeChange / ChangeSet
# ----------------------------------------------------------------------------------
def test_node_changes_and_inverse():
    nc1 = NodeChange(4, (), Remap([("ins", 0, 1)]), 3)
    nc2 = NodeChange(4, [0, 2], Remap([("dels", [1])]), 5)
    assert nc2.ipath == (0, 2)
    inv = invert_node_changes([nc1, nc2])
    assert [n.ipath for n in inv] == [(0, 2), ()]
    assert [n.count for n in inv] == [6, 2]
    assert inv[0].remap.ops == (("inss", array("Q", [1])),)
    assert inv[1].remap.map(0) is None and inv[1].remap.map(1) == 0
    # the step also moved records: records are mapped back; records the step inserted are dropped
    rec_remap = Remap([("ins", 0, 2)])            # two records inserted at 0: record 4 was record 2
    nc3 = NodeChange(1, (), Remap([("ins", 0, 1)]), 1)   # a node change inside an inserted record
    inv = invert_node_changes([nc1, nc3], rec_remap)
    assert [(n.record, n.count) for n in inv] == [(2, 2)]
    wrapper = NodeChange(None, (1,), Remap([("del", 0, 1)]), 0)
    assert invert_node_changes([wrapper], rec_remap)[0].record is None
    with pytest.raises(TypeError):
        NodeChange(0, (), "x", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        NodeChange(-1, (), Remap(), 1)


def test_change_set_normalizes():
    st0 = OverlayState.initial(5, None)
    cs = ChangeSet(st0, "edit", "Qiymat o'zgartirildi", {1, 2}, Remap([("ins", 0, 0)]))
    assert cs.records == frozenset({1, 2}) and cs.remap is None and cs.gen == st0.gen
    assert cs.nodes == () and not cs.reset and cs.select is None
    many = ChangeSet(st0, "bulk", "x", range(ov.RECORDS_MAX + 1), None)
    assert many.records is None
    exact = ChangeSet(st0, "bulk", "x", range(ov.RECORDS_MAX), None)
    assert len(exact.records) == ov.RECORDS_MAX
    assert ChangeSet(st0, "bulk", "x", range(10 ** 9), None).records is None     # never materialized
    assert ChangeSet(st0, "bulk", "x", iter(range(ov.RECORDS_MAX + 5)), None).records is None
    reset = ChangeSet(st0, "discard", "", None, None, reset=True)
    assert reset.reset and reset.records is None
    nodes = ChangeSet(st0, "edit", "x", [0], Remap([("del", 1, 1)]),
                      nodes=[NodeChange(0, (), Remap([("del", 0, 1)]), 2)])
    assert isinstance(nodes.nodes, tuple) and nodes.remap is not None
    with pytest.raises(ValueError):
        ChangeSet(st0, "bogus", "", None, None)
    with pytest.raises(TypeError):
        ChangeSet(st0, "edit", "", None, "remap")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ChangeSet("state", "edit", "", None, None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ChangeSet(st0, "edit", "", None, None, nodes=("x",))  # type: ignore[arg-type]
    for kind in ov.CHANGE_KINDS:
        assert ChangeSet(st0, kind, "", None, None).kind == kind


# ----------------------------------------------------------------------------------
# DictLayer
# ----------------------------------------------------------------------------------
spans = st.tuples(st.integers(0, U64_MAX), st.integers(0, U64_MAX)).map(lambda t: (min(t), max(t)))
rid_values = st.one_of(st.integers(0, 60), st.integers(0, 1 << 22), st.integers(U64_MAX - 5, U64_MAX))


def check_dict_layer(layer: DictLayer, model: dict, data) -> None:
    assert len(layer) == len(model)
    keys = sorted(model)
    assert list(layer.items()) == [(k, model[k]) for k in keys]
    for k in keys:
        assert layer.get(k) == model[k] and k in layer
    for probe in (61, 1 << 23, -1):
        if probe not in model:
            assert layer.get(probe) is None and probe not in layer
    assert "x" not in layer
    for _ in range(4):
        lo = data.draw(st.one_of(st.integers(0, 70), st.integers(0, 1 << 23), st.sampled_from(keys or [0])))
        hi = data.draw(st.one_of(st.integers(0, 70), st.integers(0, 1 << 24), st.just(U64_MAX + 1)))
        want = [k for k in keys if lo <= k < hi]
        got = layer.keys_in(lo, hi)
        assert isinstance(got, array) and got.typecode == "Q" and got.tolist() == want
        rids, pairs = layer.spans_in(lo, hi)
        assert rids.tolist() == want and pairs.tolist() == [v for k in want for v in model[k]]
    again = DictLayer.from_bytes(layer.to_bytes())
    assert list(again.items()) == list(layer.items()) and len(again) == len(layer)


@HYP
@given(data=st.data())
def test_dict_layer_matches_dict(data):
    layer, model = DictLayer(), {}
    versions = [(layer, {})]
    for _ in range(data.draw(st.integers(1, 6))):
        changes = data.draw(st.dictionaries(rid_values, st.one_of(st.none(), spans), max_size=25))
        new = layer.with_changes(changes)
        if not changes:
            assert new is layer
        layer = new
        for k, v in changes.items():
            if v is None:
                model.pop(k, None)
            else:
                model[k] = v
        versions.append((layer, dict(model)))
    for lyr, m in versions:
        check_dict_layer(lyr, m, data)


def test_dict_layer_validation_and_helpers():
    layer = DictLayer.from_items([(5, (1, 2)), (7, [3, 4]), (5, (9, 9))])
    assert list(layer.items()) == [(5, (9, 9)), (7, (3, 4))]
    assert layer.with_changes({99: None}).items().__next__() == (5, (9, 9))
    assert len(layer.with_changes({99: None})) == 2
    for bad in [{-1: (1, 2)}, {1: (3, 2)}, {1: (1,)}, {1: (-1, 2)}, {1: "ab"}, {U64_MAX + 1: (1, 2)},
                {1: (0, U64_MAX + 1)}]:
        with pytest.raises((ValueError, TypeError)):
            layer.with_changes(bad)
    assert layer.keys_in(10, 5).tolist() == [] and DictLayer().keys_in(0, 10).tolist() == []
    with pytest.raises(ValueError):
        DictLayer.from_bytes(b"\x00" * 23)
    with pytest.raises(ValueError):              # rids not ascending
        DictLayer.from_bytes(array("Q", [5, 1, 2, 3, 1, 2]).tobytes())
    with pytest.raises(ValueError):              # vstart > vend
        DictLayer.from_bytes(array("Q", [5, 3, 2]).tobytes())
    assert len(DictLayer.from_bytes(b"")) == 0
    assert "DictLayer" in repr(layer)


# ----------------------------------------------------------------------------------
# OverlayState: runs() / contents() vs a per-record classification
# ----------------------------------------------------------------------------------
_layer_counter = [0]


def make_array_layer(tmp_path: Path, mapping: dict[int, tuple[int, int]]) -> ArrayLayer:
    _layer_counter[0] += 1
    path = tmp_path / f"layer-{_layer_counter[0]}.bin"
    items = list(mapping.items())
    random.Random(len(items)).shuffle(items)          # any input order
    write_array_layer(path, [(r, s, e) for r, (s, e) in items], flags=0, segments=(1,), work_dir=tmp_path)
    return ArrayLayer.open(path)


@HYP
@given(data=st.data())
def test_runs_and_contents_match_naive_classification(tmp_path, monkeypatch, data):
    window = data.draw(st.sampled_from([1, 2, 3, 5, 8, 65536]))
    monkeypatch.setattr(ov, "WINDOW", window)
    monkeypatch.setattr(ov, "CLUSTER_GAP", data.draw(st.sampled_from([0, 1, 4, 65536])))
    base = data.draw(st.integers(0, 30))
    extra = data.draw(st.integers(0, 8))
    universe = list(range(base + extra))
    chosen = [r for r in universe if data.draw(st.integers(0, 4))]
    # cut into blocks of consecutive rids, shuffle the blocks -> pieces (some span base_count)
    blocks, cur = [], []
    for r in chosen:
        if cur and (r != cur[-1] + 1 or not data.draw(st.integers(0, 3))):
            blocks.append(cur)
            cur = []
        cur.append(r)
    if cur:
        blocks.append(cur)
    order = data.draw(st.permutations(range(len(blocks))))
    model = [r for i in order for r in blocks[i]]
    # layers (oldest first; the last one is a DictLayer); new rids must have content somewhere
    nlayers = data.draw(st.integers(1, 3))
    maps: list[dict[int, tuple[int, int]]] = []
    for _ in range(nlayers):
        m = {}
        for r in range(base + extra + 3):
            if not data.draw(st.integers(0, 3)):
                s = data.draw(st.integers(0, 1 << 50))
                m[r] = (s, s + data.draw(st.integers(0, 9)))
        maps.append(m)
    for r in model:
        if r >= base and not any(r in m for m in maps):
            maps[-1][r] = (EDIT_BASE + r, EDIT_BASE + r + 1)
    kinds = [data.draw(st.sampled_from(["dict", "array"])) for _ in range(nlayers - 1)] + ["dict"]
    layers = []
    try:
        for m, kind in zip(maps, kinds):
            layers.append(DictLayer.from_items(m.items()) if kind == "dict" else make_array_layer(tmp_path, m))
        state = OverlayState(base_count=base, pieces=table_of(model), layers=tuple(layers))
        n = len(model)

        def naive_content(rid: int):
            for m in reversed(maps):
                if rid in m:
                    return m[rid]
            return None

        def pure(rid: int) -> bool:
            return rid < base and naive_content(rid) is None

        for r in range(base + extra + 3):
            assert state.content(r) == naive_content(r)
            assert state.is_pure(r) == pure(r)
        pos0 = data.draw(st.integers(0, n))
        pos1 = data.draw(st.one_of(st.none(), st.integers(pos0, n + 3)))
        end = n if pos1 is None else min(n, pos1)
        runs = list(state.runs(pos0, pos1))
        expect_pos = pos0
        for i, (p, r0, k, is_pure) in enumerate(runs):
            assert p == expect_pos and 1 <= k <= window
            assert model[p:p + k] == list(range(r0, r0 + k))
            assert all(pure(r) == is_pure for r in range(r0, r0 + k))
            # never two neighbouring mergeable runs inside one window
            if i:
                pp, pr, pk, ppure = runs[i - 1]
                if (p - pos0) // window == (pp - pos0) // window:
                    assert not (ppure == is_pure and pr + pk == r0)
            # the windows are respected
            assert (p - pos0) // window == (p + k - 1 - pos0) // window
            if not is_pure:
                got = state.contents(r0, k).tolist()
                assert got == [v for r in range(r0, r0 + k) for v in naive_content(r)]
            else:
                with pytest.raises(ValueError):
                    state.contents(r0, k)
            expect_pos += k
        assert expect_pos == max(pos0, end)
    finally:
        for layer in layers:
            if isinstance(layer, ArrayLayer):
                layer.close()


def test_contents_errors_and_fast_paths(tmp_path):
    arr_layer = make_array_layer(tmp_path, {r: (EDIT_BASE + 10 * r, EDIT_BASE + 10 * r + 5) for r in range(10)})
    try:
        top = DictLayer.from_items([(3, (1, 2)), (20, (7, 8))])
        state = OverlayState(base_count=10, pieces=table_of(list(range(10)) + [20, 21]),
                             layers=(arr_layer, top))
        assert state.contents(0, 3).tolist() == [EDIT_BASE + v for r in range(3) for v in (10 * r, 10 * r + 5)]
        assert state.contents(2, 2).tolist() == [EDIT_BASE + 20, EDIT_BASE + 25, 1, 2]   # newest wins
        assert state.contents(5, 0).tolist() == []
        assert state.contents(20, 1).tolist() == [7, 8]
        with pytest.raises(EditError) as err:       # rid 21 is new and has no content: damaged state
            state.contents(20, 2)
        assert err.value.key == "edits.error.layer_corrupt"
        assert list(state.runs()) == [(0, 0, 10, False), (10, 20, 2, False)]
        plain = OverlayState(base_count=10, pieces=table_of([8, 9, 10, 11, 0]), layers=(DictLayer(),))
        assert list(plain.runs()) == [(0, 8, 2, True), (2, 10, 2, False), (4, 0, 1, True)]
        with pytest.raises(ValueError):
            plain.contents(8, 1)
        with pytest.raises(EditError):
            plain.contents(10, 1)
        assert list(plain.runs(5)) == [] and list(plain.runs(1, 3)) == [(1, 9, 1, True), (2, 10, 1, False)]
        with pytest.raises(ValueError):
            list(plain.runs(-1))
    finally:
        arr_layer.close()


# ----------------------------------------------------------------------------------
# OverlayState: construction, generations
# ----------------------------------------------------------------------------------
def test_initial_state_and_derive(tmp_path):
    reader = DatReader(tmp_path)
    s0 = OverlayState.initial(7, reader)
    assert s0.count == 7 and s0.base_count == 7 and s0.next_rid == 7 and s0.version == 0
    assert not s0.edited and s0.gen > 0 and s0.reader is reader
    assert len(s0.layers) == 1 and isinstance(s0.top, DictLayer) and s0.pieces == PieceTable.identity(7)
    assert s0.rid_at(3) == 3 and s0.rids(5, 9).tolist() == [5, 6] and s0.rids_at([6, 0]).tolist() == [6, 0]
    assert list(s0.runs()) == [(0, 0, 7, True)]
    s1 = s0.derive(version=1, flags=4)
    assert s1.gen > s0.gen and s1.edited and s1.flags == 4 and s1.pieces is s0.pieces
    s2 = s1.with_content({3: (EDIT_BASE, EDIT_BASE + 2)}, version=2)
    assert s2.gen > s1.gen and s2.content(3) == (EDIT_BASE, EDIT_BASE + 2) and s1.content(3) is None
    assert s2.layers[:-1] == s1.layers[:-1] and s2.version == 2
    assert not s2.is_pure(3) and s2.is_pure(4) and not s2.is_pure(7)
    assert hash(s2) != hash(s1) and s2 != s1 and s2 == s2
    with pytest.raises(TypeError):
        s1.derive(gen=5)
    with pytest.raises(ValueError):              # replace() would copy the gen of a live state
        dataclasses.replace(s1, version=9)
    s3 = dataclasses.replace(s1, version=9, gen=0)
    assert s3.gen > s2.gen
    g = next_gen()
    s4 = OverlayState(base_count=1, pieces=PieceTable.identity(1), layers=[DictLayer()], gen=g)
    assert s4.gen == g and isinstance(s4.layers, tuple)
    with pytest.raises(ValueError):              # same gen while s4 lives
        OverlayState(base_count=1, pieces=PieceTable.identity(1), layers=(DictLayer(),), gen=g)
    with pytest.raises(ValueError):              # never issued
        OverlayState(base_count=1, pieces=PieceTable.identity(1), layers=(DictLayer(),), gen=next_gen() + 1000)
    reader.close()


def test_state_validation():
    p = PieceTable.identity(2)
    ok = OverlayState(base_count=2, pieces=p, layers=(DictLayer(),))
    assert ok.next_rid == 2                       # raised to base_count
    assert OverlayState(base_count=2, pieces=p, layers=(DictLayer(),), next_rid=9).next_rid == 9
    w = OverlayState(base_count=2, pieces=p, layers=(DictLayer(),), wrapper=[EDIT_BASE, EDIT_BASE + 9],
                     wrapper_hole=(EDIT_BASE + 2, EDIT_BASE + 4))
    assert w.wrapper == (EDIT_BASE, EDIT_BASE + 9) and w.wrapper_hole == (EDIT_BASE + 2, EDIT_BASE + 4)
    bad_cases = [
        dict(layers=()), dict(layers=(DictLayer(), "x")), dict(layers=("x", DictLayer())),
        dict(pieces="p"), dict(base_count=-1), dict(flags=-1), dict(version=-2), dict(next_rid=-1),
        dict(wrapper=(5, 3)), dict(wrapper_hole=(1, 2)), dict(wrapper=(10, 20), wrapper_hole=(5, 12)),
        dict(reader="r"),
    ]
    for bad in bad_cases:
        kw = dict(base_count=2, pieces=p, layers=(DictLayer(),))
        kw.update(bad)
        with pytest.raises((ValueError, TypeError)):
            OverlayState(**kw)


def test_state_read_dispatch(tmp_path):
    (tmp_path / segment_name(0)).write_bytes(b"0123456789")
    reader = DatReader(tmp_path)
    reader.publish(0, 10)
    s = OverlayState.initial(0, reader)
    assert s.read(edit_offset(0, 2), edit_offset(0, 5)) == b"234"
    with pytest.raises(EditError):
        OverlayState.initial(0, None).read(edit_offset(0, 2), edit_offset(0, 5))
    reader.close()


def test_next_gen_is_unique_and_increasing_across_threads():
    results: list[list[int]] = [[] for _ in range(8)]

    def work(i: int) -> None:
        results[i] = [next_gen() for _ in range(5000)]

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    flat = [g for r in results for g in r]
    assert len(set(flat)) == len(flat) and min(flat) > 0
    for r in results:
        assert r == sorted(r)
    assert next_gen() > max(flat)
