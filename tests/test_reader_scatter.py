"""Scattered block reads of ``Document.iter_blocks`` ("Reading records" in the
:mod:`jsonreshape.reader` docstring): transient mappings vs. reads, AGENT_PROMPT §1.7.

* every block of random / duplicated / descending / sparse ascending selections, with
  member records and huge records, equals an independent reference built from the file
  bytes, is identical with the mapping path and the read path, and the progress equals
  the bytes of the selected records (never more than the selection);
* no mapping of the data file is live while the consumer holds a block, nor after the
  iteration ends, is closed early, broken out of, or ``close()`` (spy on ``mmap``); on
  Windows another process can truncate, rewrite in place and safe-save over the source
  right away (the same probes fail while a mapping is deliberately held);
* an external truncation between blocks raises ``FileChangedError`` (both paths,
  scattered and contiguous, down to an empty file); on Windows a truncation attempted while
  a block is mapped is refused by the OS and the block comes out intact;
* an unmappable file falls back to reads; per-thread handles; ``close()`` waits for a read
  in progress; cancellation; ``_read`` (exact reads + read-ahead window) returns the bytes.
"""

from __future__ import annotations

import mmap
import os
import random
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import reader
from jsonreshape.errors import Cancelled, FileChangedError
from jsonreshape.fileio import open_shared
from jsonreshape.reader import Block, Document
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows file-mapping semantics")

# reader constants squeezed so that small test files exercise every branch: a mapping per
# few records, records read inside the mapped path, sparse ascending selections in
# scattered mode, coalesced groups split by the size cap and by the density rule
TIGHT = {"MAP_MIN_RECORDS": 2, "MAP_BATCH_BYTES": 4096, "MAP_MAX_RECORD": 200, "SCATTER_GAP": 512,
         "SCATTER_GROUP": 2048, "SCATTER_SMALL_GAP": 40}


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def make_doc(tmp_path: Path, n: int = 1200, fmt: str = "array", style: str = "compact", seed: int = 1,
             settings: Settings | None = None) -> tuple[Path, Document, bytes]:
    recs = jsongen.records(n, seed=seed)
    p = tmp_path / f"{fmt}_{style}_{n}_{seed}.json"
    jsongen.write_file(p, recs, fmt, style=style)
    doc = Document.open(p, settings=settings)
    doc.build_index()
    assert doc.record_count == n
    return p, doc, p.read_bytes()


def orders(n: int, seed: int = 0) -> dict[str, list[int]]:
    rng = random.Random(seed)
    sub = rng.sample(range(n), n // 3)
    return {
        "random": rng.sample(range(n), n),
        "dupes": [rng.randrange(n) for _ in range(n)],
        "descending": list(range(n - 1, -1, -1)),
        "sparse-ascending": list(range(0, n, 7)),
        "subset-random": sub,
        "runs-and-jumps": [i for start in rng.sample(range(0, n - 5), 40) for i in range(start, start + 5)],
    }


def check_blocks(doc: Document, data: bytes, source: Any, blocks: list[Block]) -> int:
    """Every block equals the reference built from the file bytes; the indices follow the
    source. Returns the progress expected for these blocks: the bytes of the selected
    records (``Block.selected_bytes``)."""
    got: list[int] = []
    progress = 0
    for blk in blocks:
        idx = [int(i) for i in blk.indices]
        spans = [blk.span(k) for k in range(len(blk))]
        absolute = [doc.record_span(i) for i in idx]
        assert isinstance(blk.data, bytes) and len(blk.spans) == 16 * len(idx)
        if blk.huge:
            assert len(idx) == 1 and blk.data == b"" and spans == absolute and not blk.contiguous
        elif blk.contiguous:
            assert data[blk.base:blk.base + len(blk.data)] == blk.data
            assert spans == [(s - blk.base, e - blk.base) for s, e in absolute]
        else:
            pieces = [data[s:e] for s, e in absolute]
            assert blk.base == -1 and blk.data == b"\n".join(pieces)
            want, pos = [], 0
            for piece in pieces:
                want.append((pos, pos + len(piece)))
                pos += len(piece) + 1
            assert spans == want
        selected = sum(e - s for s, e in absolute)
        assert blk.selected_bytes == selected
        progress += selected
        got += idx
    assert got == [int(i) for i in source]
    return progress


def run_blocks(doc: Document, source: Any, block_bytes: int, *, mapped: bool,
               monkeypatch: pytest.MonkeyPatch) -> tuple[list[Block], int]:
    monkeypatch.setattr(reader, "MAP_SCATTERED", mapped)
    progress = Progress()
    blocks = list(doc.iter_blocks(source, block_bytes=block_bytes, progress=progress))
    assert progress.finished
    return blocks, progress.done


def block_tuple(blk: Block) -> tuple[Any, ...]:
    return blk.data, blk.indices.tobytes(), blk.spans, blk.contiguous, blk.huge, blk.base


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace the reader's ``mmap`` module: every mapping is recorded (``Spy.made``) and
    ``Spy.hook(mapping)`` runs right after a mapping is created."""
    made: list[mmap.mmap] = []

    class Spy(mmap.mmap):
        hook: Any = None

        def __new__(cls, *args: Any, **kwargs: Any) -> Spy:
            obj = super().__new__(cls, *args, **kwargs)
            made.append(obj)
            if cls.hook is not None:
                cls.hook(obj)
            return obj

    Spy.made = made  # type: ignore[attr-defined]
    monkeypatch.setattr(reader, "mmap", types.SimpleNamespace(mmap=Spy, ACCESS_READ=mmap.ACCESS_READ,
                                                              PAGESIZE=mmap.PAGESIZE))
    return Spy


def live_mappings(spy: Any) -> int:
    return sum(1 for m in spy.made if not m.closed)


_EXTERNAL = r"""
import os, sys, time

def replace(src, dst):
    # like jsonreshape.fileio._replace: scanners/indexers briefly hold freshly written files
    deadline = time.monotonic() + 2.0
    while True:
        try:
            return os.replace(src, dst)
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)

op, path = sys.argv[1], sys.argv[2]
try:
    if op == "truncate":
        with open(path, "r+b") as f:
            f.truncate(int(sys.argv[3]))
    elif op == "rewrite":                 # in place: CREATE_ALWAYS truncates first
        with open(path, "wb") as f:
            f.write(sys.argv[3].encode())
    elif op == "safe_save":               # move the old file aside, put the new one in its place
        replace(path, path + ".old")
        replace(sys.argv[3], path)
    else:
        raise SystemExit("unknown op " + op)
except OSError as exc:
    print(type(exc).__name__, exc)
    sys.exit(3)
"""


def external(op: str, path: Path, *args: Any) -> tuple[bool, str]:
    """Run a file operation in another process; ``(succeeded, output)``."""
    r = subprocess.run([sys.executable, "-c", _EXTERNAL, op, str(path), *map(str, args)],
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def replace_soon(src: Path, dst: Path) -> None:
    """``os.replace`` with a short retry for files a scanner may still hold (see _EXTERNAL)."""
    deadline = time.monotonic() + 2.0
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


# ----------------------------------------------------------------------------------
# identical blocks
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt, style", [("array", "compact"), ("array", "pretty2"), ("jsonl", "compact"),
                                        ("keyed", "compact"), ("wrapped", "pretty2")])
@pytest.mark.parametrize("tight", [False, True], ids=["default", "tight"])
def test_scattered_blocks_identical_with_both_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: Any,
                                                    fmt: str, style: str, tight: bool) -> None:
    if tight:
        for name, value in TIGHT.items():
            monkeypatch.setattr(reader, name, value)
    p, doc, data = make_doc(tmp_path, fmt=fmt, style=style)
    with doc:
        n = doc.record_count
        assert doc.member_records == (fmt == "keyed")
        for name, source in orders(n, seed=len(fmt)).items():
            for block_bytes in (700, 20_000, 1 << 20):
                spy.made.clear()
                mapped, mapped_progress = run_blocks(doc, source, block_bytes, mapped=True, monkeypatch=monkeypatch)
                used_mapping = bool(spy.made)
                assert live_mappings(spy) == 0
                spy.made.clear()
                read, read_progress = run_blocks(doc, source, block_bytes, mapped=False, monkeypatch=monkeypatch)
                assert not spy.made, "the read path never maps"
                assert [block_tuple(b) for b in mapped] == [block_tuple(b) for b in read], (name, block_bytes)
                want = check_blocks(doc, data, source, mapped)
                assert mapped_progress == read_progress == want, (name, block_bytes)
                if any(not b.contiguous and len(b) >= reader.MAP_MIN_RECORDS for b in mapped):
                    assert used_mapping, (name, block_bytes)
        # the big random selection really went through the mapping in one block
        spy.made.clear()
        run_blocks(doc, orders(n)["random"], 1 << 20, mapped=True, monkeypatch=monkeypatch)
        assert spy.made and live_mappings(spy) == 0


def test_huge_records_in_random_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: Any) -> None:
    p, doc, data = make_doc(tmp_path, n=900, settings=Settings(large_node_bytes=400), seed=4)
    with doc:
        n = doc.record_count
        huge = {i for i in range(n) if doc.is_huge(i)}
        assert 0 < len(huge) < n // 2
        for source in orders(n, seed=9).values():
            mapped, mp = run_blocks(doc, source, 30_000, mapped=True, monkeypatch=monkeypatch)
            read, rp = run_blocks(doc, source, 30_000, mapped=False, monkeypatch=monkeypatch)
            assert [block_tuple(b) for b in mapped] == [block_tuple(b) for b in read]
            assert mp == rp == check_blocks(doc, data, source, mapped)
            assert {int(b.indices[0]) for b in mapped if b.huge} == huge.intersection(source)
        assert spy.made and live_mappings(spy) == 0
        # iter_record_bytes on top of scattered blocks
        source = orders(n, seed=2)["random"]
        monkeypatch.setattr(reader, "MAP_SCATTERED", True)
        got = dict(doc.iter_record_bytes(source, include_huge=True))
        assert all(got[i] is None if i in huge else got[i] == data[slice(*doc.record_span(i))] for i in source)


def test_unmappable_file_falls_back_to_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: Any) -> None:
    p, doc, data = make_doc(tmp_path, n=600)
    with doc:
        source = orders(600, seed=5)["random"]
        read, rp = run_blocks(doc, source, 1 << 20, mapped=False, monkeypatch=monkeypatch)

        def refuse(m: mmap.mmap) -> None:
            m.close()
            raise OSError(22, "mapping not supported here")

        spy.hook = refuse
        mapped, mp = run_blocks(doc, source, 1 << 20, mapped=True, monkeypatch=monkeypatch)
        assert spy.made and live_mappings(spy) == 0
        assert [block_tuple(b) for b in mapped] == [block_tuple(b) for b in read] and mp == rp


# ----------------------------------------------------------------------------------
# no mapping outlives a block
# ----------------------------------------------------------------------------------
def test_no_mapping_is_live_between_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: Any) -> None:
    monkeypatch.setattr(reader, "MAP_SCATTERED", True)
    p, doc, data = make_doc(tmp_path, n=1500)
    with doc:
        source = orders(1500, seed=3)["random"]
        # consumer holds each block: nothing is mapped at any yield
        count = 0
        for blk in doc.iter_blocks(source, block_bytes=40_000):
            assert live_mappings(spy) == 0
            count += 1
        assert count > 3 and spy.made and live_mappings(spy) == 0
        # paused (suspended generator), closed early, broken out of
        gen = doc.iter_blocks(source, block_bytes=40_000)
        next(gen)
        assert live_mappings(spy) == 0
        gen.close()
        assert live_mappings(spy) == 0
        it = doc.iter_blocks(source, block_bytes=40_000)
        for _ in it:
            break
        assert live_mappings(spy) == 0  # the generator object is still alive here
        del it
    assert doc.closed and live_mappings(spy) == 0


@windows_only
def test_held_mapping_blocks_the_probes(tmp_path: Path) -> None:
    """Control for the tests below: on Windows a live mapping of the file (even through a
    fully shared handle) makes another process's truncation and in-place rewrite fail."""
    p = tmp_path / "held.json"
    p.write_bytes(b"[" + b",".join(b"%d" % i for i in range(20000)) + b"]")
    size = p.stat().st_size
    with open_shared(p, "rb", buffering=0) as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            assert m[:1] == b"["
            ok, out = external("truncate", p, size // 2)
            assert not ok, out
            ok, out = external("rewrite", p, "[1]")
            assert not ok, out
        ok, out = external("truncate", p, size // 2)  # the shared handle alone does not block
        assert ok, out
    assert p.stat().st_size == size // 2


@windows_only
@pytest.mark.parametrize("how", ["finished", "closed-early", "paused", "break"])
def test_source_can_be_changed_by_another_process_after_iteration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                  spy: Any, how: str) -> None:
    monkeypatch.setattr(reader, "MAP_SCATTERED", True)
    p, doc, data = make_doc(tmp_path, n=1500)
    with doc:
        source = orders(1500, seed=6)["random"]
        gen = doc.iter_blocks(source, block_bytes=40_000)
        first = next(gen)
        assert not first.contiguous and len(first) >= reader.MAP_MIN_RECORDS and spy.made
        if how == "finished":
            for _ in gen:
                pass
        elif how == "closed-early":
            gen.close()
        elif how == "break":
            for _ in gen:
                break
        # "paused": the generator stays suspended after its first block
        assert live_mappings(spy) == 0
        size = p.stat().st_size
        ok, out = external("truncate", p, size - 1)
        assert ok, out
        assert p.stat().st_size == size - 1
        ok, out = external("rewrite", p, "[1, 2]")
        assert ok, out
        assert p.read_bytes() == b"[1, 2]"
        newer = tmp_path / "newer.json"
        newer.write_bytes(b"[3]")
        ok, out = external("safe_save", p, newer)
        assert ok, out
        assert p.read_bytes() == b"[3]" and Path(str(p) + ".old").exists()
        if how == "paused":
            with pytest.raises(FileChangedError):   # the data under the open handle shrank
                for _ in gen:
                    pass
    # all handles released by close(): even a plain rename-over (MoveFileEx) works now
    last = tmp_path / "last.json"
    last.write_bytes(b"[4]")
    replace_soon(last, Path(str(p) + ".old"))
    assert Path(str(p) + ".old").read_bytes() == b"[4]"


@windows_only
def test_truncation_while_a_block_is_mapped_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       spy: Any) -> None:
    monkeypatch.setattr(reader, "MAP_SCATTERED", True)
    p, doc, data = make_doc(tmp_path, n=1500)
    attempts: list[tuple[bool, str]] = []

    def truncate_now(m: mmap.mmap) -> None:
        if not attempts:
            attempts.append(external("truncate", p, 10))

    spy.hook = truncate_now
    with doc:
        source = orders(1500, seed=8)["random"]
        blocks = list(doc.iter_blocks(source, block_bytes=1 << 20))
        assert attempts and not attempts[0][0], attempts   # refused while the view was mapped
        check_blocks(doc, data, source, blocks)              # ... and the block is intact
        assert live_mappings(spy) == 0
        spy.hook = None
        ok, out = external("truncate", p, 10)               # allowed once the block is done
        assert ok, out
        with pytest.raises(FileChangedError):
            list(doc.iter_blocks(source, block_bytes=1 << 20))


# ----------------------------------------------------------------------------------
# truncation between blocks
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("mapped", [True, False], ids=["map", "read"])
@pytest.mark.parametrize("cut", ["half", "empty"])
@pytest.mark.parametrize("kind", ["random", "contiguous"])
def test_truncation_between_blocks_raises_file_changed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: Any,
                                                        mapped: bool, cut: str, kind: str) -> None:
    monkeypatch.setattr(reader, "MAP_SCATTERED", mapped)
    p, doc, data = make_doc(tmp_path, n=1500)
    with doc:
        source = orders(1500, seed=11)["random"] if kind == "random" else None
        gen = doc.iter_blocks(source, block_bytes=40_000)
        first = next(gen)
        assert first.contiguous == (kind == "contiguous")
        with open(p, "r+b") as f:   # another writer (on Windows this would fail if a view were mapped)
            f.truncate(0 if cut == "empty" else len(data) // 2)
        with pytest.raises(FileChangedError):
            for _ in gen:
                pass
        assert live_mappings(spy) == 0
        assert doc.check_changed()


# ----------------------------------------------------------------------------------
# threads, close(), cancellation
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("mapped", [True, False], ids=["map", "read"])
def test_threads_use_their_own_handles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mapped: bool) -> None:
    monkeypatch.setattr(reader, "MAP_SCATTERED", mapped)
    p, doc, data = make_doc(tmp_path, n=1500, fmt="jsonl")
    errors: list[str] = []
    with doc:
        def worker(seed: int) -> None:
            try:
                for _ in range(3):
                    source = orders(1500, seed=seed)["random"]
                    check_blocks(doc, data, source, list(doc.iter_blocks(source, block_bytes=30_000)))
                    for i in random.Random(seed).sample(range(1500), 200):
                        if doc.record_bytes(i) != data[slice(*doc.record_span(i))]:
                            errors.append(f"record {i}")
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert not errors, errors[:3]
        assert len(doc._handles) >= 6
    assert doc._handles == []


def test_close_waits_for_a_read_in_progress(tmp_path: Path) -> None:
    p, doc, data = make_doc(tmp_path, n=50)
    h = doc._fh()
    h.lock.acquire()   # as during a read or a mapping through this thread's handle
    closer = threading.Thread(target=doc.close)
    closer.start()
    time.sleep(0.2)
    assert closer.is_alive() and not h.closed
    h.lock.release()
    closer.join(10)
    assert not closer.is_alive() and h.closed and doc.closed


@pytest.mark.parametrize("mapped", [True, False], ids=["map", "read"])
def test_close_with_a_paused_iteration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spy: Any,
                                       mapped: bool) -> None:
    monkeypatch.setattr(reader, "MAP_SCATTERED", mapped)
    p, doc, data = make_doc(tmp_path, n=1500)
    gen = doc.iter_blocks(orders(1500, seed=12)["random"], block_bytes=40_000)
    next(gen)
    doc.close()
    assert live_mappings(spy) == 0 and doc._handles == []
    with pytest.raises(ValueError):
        next(gen)
    gen.close()
    replace_soon(p, p.with_suffix(".moved"))   # nothing holds the file any more


def test_cancel_during_scattered_iteration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "MAP_SCATTERED", True)
    p, doc, data = make_doc(tmp_path, n=3000)
    with doc:
        token = CancelToken()
        gen = doc.iter_blocks(orders(3000, seed=13)["random"], block_bytes=20_000, cancel=token)
        next(gen)
        token.cancel()
        with pytest.raises(Cancelled):
            for _ in gen:
                pass


# ----------------------------------------------------------------------------------
# _read: exact reads and the read-ahead window
# ----------------------------------------------------------------------------------
def test_read_returns_exact_bytes_for_every_access_pattern(tmp_path: Path) -> None:
    p, doc, data = make_doc(tmp_path, n=1500, style="pretty2")
    with doc:
        n = doc.record_count
        size = len(data)
        rng = random.Random(21)
        patterns = {
            "sequential": list(range(n)),
            "backward": list(range(n - 1, -1, -1)),
            "random": [rng.randrange(n) for _ in range(2000)],
            "every-3rd": list(range(0, n, 3)),
        }
        for name, idx in patterns.items():
            for i in idx:
                assert doc.record_bytes(i) == data[slice(*doc.record_span(i))], (name, i)
        # forward walks of small overlapping probes, reads across the window end, big reads
        for start in range(0, size - 9000, 997):
            assert doc._read(start, start + 4096) == data[start:start + 4096]
        for step in (1, 100, 4000, 8192, 8193, 40000):
            pos = 0
            while pos + step <= size:
                assert doc._read(pos, pos + step) == data[pos:pos + step], step
                pos += step + 3
        assert doc._read(size - 5, size) == data[-5:]
        assert doc._read(7, 7) == b""
        with pytest.raises(ValueError):
            doc._read(10, 5)
        with pytest.raises(ValueError):
            doc._read(-1, 5)
        with pytest.raises(FileChangedError):
            doc._read(0, size + 1)
        assert len(doc._handles) == 1   # one thread, one handle


def test_read_after_truncation_raises_file_changed(tmp_path: Path) -> None:
    p, doc, data = make_doc(tmp_path, n=600)
    with doc:
        last = doc.record_count - 1
        s, e = doc.record_span(last)
        assert s > 2 * reader.READ_AHEAD
        assert doc.record_bytes(0) == data[slice(*doc.record_span(0))]
        with open(p, "r+b") as f:
            f.truncate(s)
        with pytest.raises(FileChangedError):   # an exact read
            doc.record_bytes(last)
        assert doc._read(s - 200, s - 100) == data[s - 200:s - 100]
        with pytest.raises(FileChangedError):   # continues the walk: the read-ahead comes back short
            doc._read(s - 100, s + 10)
        assert doc._read(s - 300, s) == data[s - 300:s]
        assert doc._read(0, 10) == data[:10]
