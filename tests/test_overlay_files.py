"""jsonreshape.overlay, file-backed parts (docs/ARCHITECTURE.md §12.1, §12.2 "Files").

* ``write_array_layer`` / ``ArrayLayer``: round trip for entries in any order (sorted, natural runs, random,
  reversed -> block sorts, spilled runs beyond ``SPILL_ENTRIES``), duplicate rids (the last one wins), the
  exact file format (header, crc32, segment list, 64-byte padding), damaged files (truncated, appended,
  wrong crc, magic, version, padding, count, segment list, missing) -> ``EditError``; the file (and its
  whole folder) can be deleted or renamed while it is mapped and reads keep working (Windows sharing);
  close / use after close; cancellation and invalid input leave no temporary files;
* ``DatReader``: published bounds (beyond the end, crossing segments, reversed spans, non-edit offsets,
  missing and truncated segments -> ``EditError``), monotonic publish, forget, open_segment, close,
  per-thread handles pruned when threads end, 8 reader threads while a writer appends and publishes,
  segment files deleted while handles are open.
"""

from __future__ import annotations

import os
import random
import shutil
import struct
import threading
import zlib
from array import array
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from jsonreshape import overlay as ov
from jsonreshape.errors import Cancelled, EditError
from jsonreshape.fileio import open_shared
from jsonreshape.overlay import (
    EDIT_BASE,
    LAYER_MAGIC,
    ArrayLayer,
    DatReader,
    edit_offset,
    segment_name,
    write_array_layer,
)
from jsonreshape.tasks import CancelToken

HYP = settings(derandomize=True, database=None, deadline=None, max_examples=80,
               suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
U64_MAX = (1 << 64) - 1
_counter = [0]


def fresh(tmp_path: Path, stem: str = "layer") -> Path:
    _counter[0] += 1
    return tmp_path / f"{stem}-{_counter[0]}.bin"


def last_wins(entries) -> dict[int, tuple[int, int]]:
    model: dict[int, tuple[int, int]] = {}
    for r, s, e in entries:
        model[r] = (s, e)
    return model


def check_layer(layer: ArrayLayer, model: dict[int, tuple[int, int]], rng: random.Random) -> None:
    keys = sorted(model)
    assert layer.count == len(model) == len(layer)
    assert list(layer.items()) == [(k, model[k]) for k in keys]
    for k in keys:
        assert layer.get(k) == model[k] and k in layer
    for probe in {0, 1, U64_MAX, (keys[-1] + 1) if keys else 5} - set(keys):
        assert layer.get(probe) is None and probe not in layer
    assert "1" not in layer
    bounds = [0, 1, U64_MAX, U64_MAX + 1] + keys + [k + 1 for k in keys]
    for _ in range(12):
        lo, hi = rng.choice(bounds), rng.choice(bounds)
        want = [k for k in keys if lo <= k < hi]
        got = layer.keys_in(lo, hi)
        assert isinstance(got, array) and got.typecode == "Q" and got.tolist() == want
        rids, pairs = layer.spans_in(lo, hi)
        assert rids.tolist() == want and pairs.tolist() == [v for k in want for v in model[k]]


spans = st.tuples(st.integers(0, U64_MAX), st.integers(0, U64_MAX)).map(lambda t: (min(t), max(t)))


@HYP
@given(data=st.data())
def test_write_and_open_round_trip(tmp_path, monkeypatch, data):
    monkeypatch.setattr(ov, "SPILL_ENTRIES", data.draw(st.sampled_from([3, 10, 4 * 1024 * 1024])))
    monkeypatch.setattr(ov, "MERGE_RUNS_MAX", data.draw(st.sampled_from([1, 2, 64])))
    monkeypatch.setattr(ov, "SORT_BLOCK", data.draw(st.sampled_from([2, 5, 1024 * 1024])))
    monkeypatch.setattr(ov, "_BLOCK", data.draw(st.sampled_from([2, 7, 65536])))
    monkeypatch.setattr(ov, "MERGE_FANIN", data.draw(st.sampled_from([2, 3, 64])))
    rid_st = st.one_of(st.integers(0, 40), st.integers(0, U64_MAX))
    raw = data.draw(st.lists(st.tuples(rid_st, spans), max_size=60))
    entries = [(r, s, e) for r, (s, e) in raw]
    shape = data.draw(st.sampled_from(["as drawn", "sorted", "reversed", "runs"]))
    if shape == "sorted":
        entries.sort(key=lambda t: t[0])                 # stable: duplicates keep their order
    elif shape == "reversed":
        entries = sorted(entries, key=lambda t: t[0])[::-1]
    elif shape == "runs":
        cut = data.draw(st.integers(0, len(entries)))
        entries = sorted(entries[:cut], key=lambda t: t[0]) + sorted(entries[cut:], key=lambda t: t[0])
    flags = data.draw(st.integers(0, 0xFFFFFFFF))
    segments = tuple(data.draw(st.lists(st.integers(0, ov.MAX_SEGMENT), max_size=12)))
    path = fresh(tmp_path)
    work = tmp_path / "work"
    count = write_array_layer(path, iter(entries), flags=flags, segments=segments, work_dir=work)
    model = last_wins(entries)
    assert count == len(model)
    layer = ArrayLayer.open(path)
    try:
        assert layer.flags == flags and layer.segments == segments
        assert layer.name == path.name and layer.path == path and not layer.closed
        check_layer(layer, model, random.Random(len(entries)))
    finally:
        layer.close()
    assert not list(tmp_path.glob("*.tmp"))
    assert not work.exists() or not any(work.iterdir())   # spilled runs are removed


def test_exact_file_format(tmp_path):
    p = tmp_path / "x.bin"
    assert write_array_layer(p, [(7, 70, 75), (2, 20, 22)], flags=0xABCD, segments=(3, 9), work_dir=None) == 2
    raw = p.read_bytes()
    assert raw[:8] == LAYER_MAGIC == b"JRLAYER\0"
    version, flags, count, crc, nsegs = struct.unpack_from("<IIQII", raw, 8)
    assert (version, flags, count, nsegs) == (1, 0xABCD, 2, 2)
    assert struct.unpack_from("<2I", raw, 32) == (3, 9)
    assert len(raw) == 64 + 24 * 2 and raw[40:64] == bytes(24)          # padded to 64 bytes
    assert array("Q", raw[64:80]).tolist() == [2, 7]
    assert array("Q", raw[80:]).tolist() == [20, 22, 70, 75]
    assert crc == zlib.crc32(raw[64:])                                   # crc32 of the arrays
    p9 = tmp_path / "nine.bin"
    write_array_layer(p9, [(1, 2, 3)], flags=0, segments=range(9), work_dir=None)
    raw9 = p9.read_bytes()
    assert len(raw9) == 128 + 24 and struct.unpack_from("<9I", raw9, 32) == tuple(range(9))
    p0 = tmp_path / "empty.bin"
    assert write_array_layer(p0, [], flags=0, segments=(), work_dir=None) == 0
    assert len(p0.read_bytes()) == 64
    layer = ArrayLayer.open(p0)
    try:
        assert layer.count == 0 and layer.get(0) is None and layer.keys_in(0, U64_MAX).tolist() == []
        assert list(layer.items()) == [] and layer.spans_in(0, 10) == (array("Q"), array("Q"))
    finally:
        layer.close()
    with pytest.raises(TypeError):
        ArrayLayer()


@pytest.mark.parametrize("damage", ["truncate", "append", "rid_bit", "pair_bit", "magic", "version", "padding",
                                    "short", "empty", "count", "nsegs", "crc_field"])
def test_damaged_layer_files_raise_edit_error(tmp_path, damage):
    p = tmp_path / "d.bin"
    write_array_layer(p, [(i, i * 10, i * 10 + 3) for i in range(20)], flags=1, segments=(0,), work_dir=None)
    raw = bytearray(p.read_bytes())
    if damage == "truncate":
        raw = raw[:-1]
    elif damage == "append":
        raw += b"\0"
    elif damage == "rid_bit":
        raw[64 + 5] ^= 0x10
    elif damage == "pair_bit":
        raw[-3] ^= 0x01
    elif damage == "magic":
        raw[0:8] = b"JRLAYEX\0"
    elif damage == "version":
        struct.pack_into("<I", raw, 8, 2)
    elif damage == "padding":
        raw[40] = 1
    elif damage == "short":
        raw = raw[:20]
    elif damage == "empty":
        raw = bytearray()
    elif damage == "count":
        struct.pack_into("<Q", raw, 16, 21)
    elif damage == "nsegs":
        struct.pack_into("<I", raw, 28, 1 << 21)
    else:
        struct.pack_into("<I", raw, 24, 12345)
    p.write_bytes(raw)
    with pytest.raises(EditError) as err:
        ArrayLayer.open(p)
    assert err.value.key == "edits.error.layer_corrupt"
    assert err.value.params["path"] == str(p) and err.value.params["detail"]
    assert str(err.value)                         # a localized message


def test_missing_layer_file(tmp_path):
    with pytest.raises(EditError) as err:
        ArrayLayer.open(tmp_path / "nope.bin")
    assert err.value.key == "edits.error.layer_corrupt"


def test_layer_file_and_folder_can_be_deleted_while_mapped(tmp_path):
    folder = tmp_path / "layerdir"
    folder.mkdir()
    p = folder / "m.bin"
    entries = [(i, EDIT_BASE + i, EDIT_BASE + 2 * i) for i in range(0, 5000, 3)]
    write_array_layer(p, entries, flags=0, segments=(0,), work_dir=None)
    model = last_wins(entries)
    layer = ArrayLayer.open(p)
    other = None
    try:
        os.remove(p)                                     # FILE_SHARE_DELETE: allowed while mapped
        assert not p.exists()
        check_layer(layer, model, random.Random(1))      # the mapped data stays readable
        write_array_layer(p, [(1, 2, 3)], flags=0, segments=(), work_dir=None)   # the name is free again
        other = ArrayLayer.open(p)
        moved = folder / "moved.bin"
        os.replace(p, moved)                             # renaming a mapped file works too
        assert other.get(1) == (2, 3) and other.count == 1
        shutil.rmtree(folder)                            # a discarded layer folder goes away at once
        assert not folder.exists()
        assert layer.get(3) == (EDIT_BASE + 3, EDIT_BASE + 6) and other.keys_in(0, 5).tolist() == [1]
    finally:
        layer.close()
        if other is not None:
            other.close()
    assert layer.closed
    layer.close()                                        # idempotent
    for call in (lambda: layer.get(1), lambda: layer.keys_in(0, 9), lambda: layer.spans_in(0, 9),
                 lambda: list(layer.items())):
        with pytest.raises(ValueError):
            call()
    assert "closed=True" in repr(layer)


def test_close_waits_for_a_held_slice(tmp_path):
    p = tmp_path / "s.bin"
    write_array_layer(p, [(1, 2, 3), (4, 5, 6)], flags=0, segments=(), work_dir=None)
    layer = ArrayLayer.open(p)
    view = layer._raw[0:8]                               # a reader that still holds a slice
    releaser = threading.Timer(0.05, view.release)
    releaser.start()
    layer.close()                                        # retries until the slice is released
    releaser.join()
    assert layer.closed


def test_cancel_and_invalid_input_leave_no_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ov, "SPILL_ENTRIES", 10)
    monkeypatch.setattr(ov, "_BLOCK", 4)
    work = tmp_path / "work"
    target = tmp_path / "c.bin"

    def leftovers() -> list[Path]:
        return [q for q in tmp_path.rglob("*") if q.is_file()]

    tok = CancelToken()

    def cancel_midway():
        for i in range(100):
            if i == 50:
                tok.cancel()
            yield (100 - i, i, i)

    with pytest.raises(Cancelled):
        write_array_layer(target, cancel_midway(), flags=0, segments=(), work_dir=work, cancel=tok)
    assert leftovers() == []
    tok2 = CancelToken()

    def cancel_at_end():                               # 40 entries = 10 full blocks: the merge phase cancels
        yield from ((40 - i, i, i) for i in range(40))
        tok2.cancel()

    with pytest.raises(Cancelled):
        write_array_layer(target, cancel_at_end(), flags=0, segments=(), work_dir=work, cancel=tok2)
    assert leftovers() == []
    for bad in ([(1, 5, 4)], [(-1, 0, 0)], [(1, 2)], ["abc"], [(1, 2, 3.5)], [(U64_MAX + 1, 0, 0)]):
        with pytest.raises(ValueError):
            write_array_layer(target, bad, flags=0, segments=(), work_dir=work)
    for flags, segs in ((-1, ()), (1 << 32, ()), (0, (-1,)), (0, (ov.MAX_SEGMENT + 1,))):
        with pytest.raises(ValueError):
            write_array_layer(target, [], flags=flags, segments=segs, work_dir=work)
    assert leftovers() == []


@pytest.mark.parametrize("spill", [7, 4 * 1024 * 1024])
def test_entry_blocks_input(tmp_path, monkeypatch, spill):
    monkeypatch.setattr(ov, "SPILL_ENTRIES", spill)
    monkeypatch.setattr(ov, "_BLOCK", 5)
    rng = random.Random(spill)
    entries = [(rng.randrange(50), rng.randrange(1000), 0) for _ in range(200)]
    entries = [(r, s, s + rng.randrange(9)) for r, s, _ in entries]
    blocks = []
    for a in range(0, len(entries), 37):                 # uneven blocks; arrays and plain lists
        part = entries[a:a + 37]
        cols = ([t[0] for t in part], [t[1] for t in part], [t[2] for t in part])
        blocks.append(tuple(array("Q", c) for c in cols) if a % 2 else cols)
    blocks.append((array("Q"), array("Q"), array("Q")))   # an empty block is fine
    p1, p2 = tmp_path / "blocks.bin", tmp_path / "tuples.bin"
    n1 = write_array_layer(p1, ov.EntryBlocks(iter(blocks)), flags=3, segments=(2,), work_dir=tmp_path / "w")
    n2 = write_array_layer(p2, entries, flags=3, segments=(2,), work_dir=tmp_path / "w")
    assert n1 == n2 == len(last_wins(entries))
    assert p1.read_bytes() == p2.read_bytes()            # identical files
    for bad in ([(array("Q", [1, 2]), array("Q", [1]), array("Q", [1, 2]))],      # lengths differ
                [(array("Q", [1]), array("Q", [5]), array("Q", [4]))],            # vstart > vend
                [(array("Q", [1]), array("Q", [5]))],                             # not a triple
                [([-1], [0], [0])], [5]):
        with pytest.raises(ValueError):
            write_array_layer(tmp_path / "bad.bin", ov.EntryBlocks(bad), flags=0, segments=(), work_dir=None)
    tok = CancelToken()
    tok.cancel()
    with pytest.raises(Cancelled):
        write_array_layer(tmp_path / "c.bin", ov.EntryBlocks(blocks), flags=0, segments=(), work_dir=None,
                          cancel=tok)
    assert sorted(q.name for q in tmp_path.iterdir() if q.is_file()) == ["blocks.bin", "tuples.bin"]


def test_sorted_input_beyond_one_batch_is_concatenated(tmp_path, monkeypatch):
    monkeypatch.setattr(ov, "SPILL_ENTRIES", 1000)
    monkeypatch.setattr(ov, "_BLOCK", 64)

    def no_merge(*_a, **_k):
        raise AssertionError("sorted input must not go through the k-way merge")

    monkeypatch.setattr(ov, "_write_merged", no_merge)
    entries = []
    for r in range(0, 30_000, 3):                        # duplicates also across batch and block boundaries
        entries.append((r, r, r + 1))
        if r % 7 == 0:
            entries.append((r, r + 100, r + 200))
    work = tmp_path / "w"
    p = tmp_path / "sorted.bin"
    assert write_array_layer(p, entries, flags=0, segments=(), work_dir=work) == 10_000
    layer = ArrayLayer.open(p)
    try:
        check_layer(layer, last_wins(entries), random.Random(9))
    finally:
        layer.close()
    assert not any(work.iterdir())


@pytest.mark.parametrize("phase", ["block_sort", "reduce", "concat"])
def test_cancel_in_every_phase_leaves_no_files(tmp_path, monkeypatch, phase):
    """The token is cancelled when the input is exhausted, so the phase after reading notices it."""
    monkeypatch.setattr(ov, "_BLOCK", 4)
    if phase == "block_sort":                    # one batch, too many runs -> block sorts
        monkeypatch.setattr(ov, "MERGE_RUNS_MAX", 1)
        rids = [(i * 37) % 64 for i in range(64)]
    elif phase == "reduce":                      # several unordered runs, tiny fan-in -> extra merge pass
        monkeypatch.setattr(ov, "SPILL_ENTRIES", 8)
        monkeypatch.setattr(ov, "MERGE_FANIN", 2)
        rids = [r for b in range(8) for r in range(b, 64, 8)]
    else:                                        # sorted beyond one batch -> concatenation
        monkeypatch.setattr(ov, "SPILL_ENTRIES", 8)
        rids = list(range(64))
    tok = CancelToken()

    def entries():
        yield from ((r, r, r + 1) for r in rids)
        tok.cancel()

    with pytest.raises(Cancelled):
        write_array_layer(tmp_path / "x.bin", entries(), flags=0, segments=(), work_dir=tmp_path / "w",
                          cancel=tok)
    assert [q for q in tmp_path.rglob("*") if q.is_file()] == []


@pytest.mark.parametrize("fanin", [2, 64])
def test_spilled_merge_with_many_duplicates(tmp_path, monkeypatch, fanin):
    monkeypatch.setattr(ov, "SPILL_ENTRIES", 1000)
    monkeypatch.setattr(ov, "SORT_BLOCK", 300)
    monkeypatch.setattr(ov, "MERGE_FANIN", fanin)
    rng = random.Random(3)
    entries = [(rng.randrange(3000), rng.randrange(1 << 40), 0) for _ in range(12_000)]
    entries = [(r, s, s + rng.randrange(100)) for r, s, _ in entries]
    p = tmp_path / "big.bin"
    work = tmp_path / "w"
    assert write_array_layer(p, entries, flags=7, segments=(1, 2), work_dir=work) == len(last_wins(entries))
    layer = ArrayLayer.open(p)
    try:
        check_layer(layer, last_wins(entries), rng)
    finally:
        layer.close()
    assert not any(work.iterdir())


# ----------------------------------------------------------------------------------
# DatReader
# ----------------------------------------------------------------------------------
def test_dat_reader_bounds(tmp_path):
    data = bytes(range(256)) * 10
    (tmp_path / segment_name(0)).write_bytes(data)
    (tmp_path / segment_name(1)).write_bytes(b"segment one")
    r = DatReader(tmp_path)
    assert "DatReader" in repr(r) and not r.closed and r.segments() == () and r.published(0) == 0
    with pytest.raises(EditError):                       # nothing published yet
        r.read(edit_offset(0, 0), edit_offset(0, 1))
    r.publish(0, 100)
    assert r.read(edit_offset(0, 10), edit_offset(0, 20)) == data[10:20]
    assert r.read(edit_offset(0, 100), edit_offset(0, 100)) == b""
    for bad in [(edit_offset(0, 90), edit_offset(0, 101)),        # beyond the published end
                (edit_offset(0, 20), edit_offset(0, 10)),         # reversed
                (edit_offset(0, 10), edit_offset(1, 5)),          # crosses segments
                (10, 20),                                         # not an edit offset
                (edit_offset(1, 0), edit_offset(1, 3))]:          # segment 1 not published
        with pytest.raises(EditError) as err:
            r.read(*bad)
        assert err.value.key == "edits.error.layer_corrupt"
    r.publish(0, len(data))
    r.publish(0, len(data))                              # same end again: fine
    assert r.read(edit_offset(0, 2000), edit_offset(0, 2560)) == data[2000:]
    with pytest.raises(ValueError):
        r.publish(0, 5)                                  # monotonic
    for bad in [(-1, 5), (ov.MAX_SEGMENT + 1, 5), (0, -1), (0, ov.SEG_SIZE_LIMIT)]:
        with pytest.raises(ValueError):
            r.publish(*bad)
    r.publish(1, 11)
    assert r.read(edit_offset(1, 8), edit_offset(1, 11)) == b"one"
    assert r.segments() == (0, 1) and r.published(1) == 11
    r.publish(7, 10)                                     # published but the file is missing
    with pytest.raises(EditError):
        r.read(edit_offset(7, 0), edit_offset(7, 5))
    with pytest.raises(EditError):
        r.open_segment(7)
    r.publish(1, 50)                                     # published beyond the real file size
    with pytest.raises(EditError):
        r.read(edit_offset(1, 5), edit_offset(1, 40))
    with r.open_segment(0) as f:                         # caller-owned handle
        assert f.read(3) == data[:3]
    r.forget(0)
    with pytest.raises(EditError):
        r.read(edit_offset(0, 0), edit_offset(0, 1))
    assert r.segments() == (1, 7)
    r.close()
    r.close()
    assert r.closed
    with pytest.raises(ValueError):
        r.read(edit_offset(1, 0), edit_offset(1, 1))


def test_dat_reader_handles_are_per_thread_and_pruned(tmp_path):
    (tmp_path / segment_name(0)).write_bytes(b"abcdef")
    r = DatReader(tmp_path)
    r.publish(0, 6)
    seen = []

    def once() -> None:
        seen.append(r.read(edit_offset(0, 1), edit_offset(0, 3)))

    for _ in range(5):
        th = threading.Thread(target=once)
        th.start()
        th.join()
    assert seen == [b"bc"] * 5
    assert len(r._registry) == 1                         # each new thread pruned the finished ones
    once()
    assert len(r._registry) == 1 and r._registry[0][0]() is threading.current_thread()
    r.close()
    assert r._registry == []


def test_segment_file_deleted_while_handles_are_open(tmp_path):
    p = tmp_path / segment_name(0)
    p.write_bytes(b"0123456789")
    r = DatReader(tmp_path)
    r.publish(0, 10)
    assert r.read(edit_offset(0, 0), edit_offset(0, 4)) == b"0123"    # this thread's handle is open
    os.remove(p)
    assert not p.exists()
    assert r.read(edit_offset(0, 4), edit_offset(0, 8)) == b"4567"    # the open handle keeps reading
    r.close()


def test_dat_reader_concurrent_reads_while_a_writer_appends(tmp_path):
    size = 1 << 20
    pattern = bytes(range(256)) * (size // 256 + 1)
    bufs = {seg: pattern[seg * 37:seg * 37 + size] for seg in (0, 1)}
    reader = DatReader(tmp_path)
    errors: list[BaseException] = []
    done = threading.Event()
    reads = [0] * 8

    def writer() -> None:
        try:
            rng = random.Random(1)
            files = {seg: open_shared(tmp_path / segment_name(seg), "ab") for seg in (0, 1)}
            written = {0: 0, 1: 0}
            try:
                while any(v < size for v in written.values()):
                    seg = rng.choice([s for s in written if written[s] < size])
                    n = min(rng.randrange(1, 60_000), size - written[seg])
                    f = files[seg]
                    f.write(bufs[seg][written[seg]:written[seg] + n])
                    f.flush()
                    os.fsync(f.fileno())
                    written[seg] += n
                    reader.publish(seg, written[seg])
            finally:
                for f in files.values():
                    f.close()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)
        finally:
            done.set()

    def read_loop(i: int) -> None:
        rng = random.Random(100 + i)
        try:
            while not done.is_set() or reads[i] < 300:
                seg = rng.randrange(2)
                end = reader.published(seg)
                if end:
                    a = rng.randrange(end)
                    b = rng.randrange(a, min(end, a + 70_000) + 1)
                    got = reader.read(edit_offset(seg, a), edit_offset(seg, b))
                    if got != bufs[seg][a:b]:
                        raise AssertionError(f"thread {i}: wrong bytes for {seg}:{a}..{b}")
                    if not reads[i] % 50:
                        try:
                            reader.read(edit_offset(seg, 0), edit_offset(seg, size + 1))
                        except EditError:
                            pass
                        else:
                            raise AssertionError("a read beyond the published end succeeded")
                reads[i] += 1
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=read_loop, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(120)
    assert not any(th.is_alive() for th in threads)
    assert not errors, errors[:3]
    assert all(n >= 300 for n in reads)
    assert reader.published(0) == size and reader.published(1) == size
    assert len(reader._registry) <= 8
    reader.close()
