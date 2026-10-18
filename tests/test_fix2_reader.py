"""Regression tests of the second fix round in the reader workstream (phase 5d).

* N05: a source that only *grows* while it is read (an active JSON Lines log) is read as
  the snapshot opened -- ``check_changed`` / ``ensure_unchanged`` and the handle checks
  accept it, ``Document.appended_bytes`` and the ``reader.warn.appended`` warning tell how
  much was appended -- while every other change (truncation, a same-size edit, a longer
  rewrite, a replacement) still raises ``FileChangedError``; library operations and one
  CLI run while another thread / process appends lines; gzip sources; a prepared copy
  holds the identified version only;
* N08: prepared copies that are protected (in use, recently used) and files in use do
  not count against ``cache_limit_bytes``, so they never make the eviction delete every
  unrelated index file;
* robustness F8 (partial before): ``FileIdentity.sample_hash`` over 16 sampled pages
  between the hashed head and tail catches a same-size edit with a restored mtime in the
  middle of a file when it touches one of them (the stale index is not used then).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from array import array
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import codec, fileio, reader
from jsonreshape.errors import FileChangedError, JsonReshapeError
from jsonreshape.fileio import (
    HEAD_HASH_BYTES,
    SAMPLE_BLOCKS,
    SAMPLE_BYTES,
    TAIL_HASH_BYTES,
    FileIdentity,
    appended_bytes,
    file_identity,
    grown_from,
    open_shared,
    prepare_data_file,
    prune_prepared,
    sample_ranges,
)
from jsonreshape.i18n import MESSAGES, t
from jsonreshape.index import IndexReader, IndexWriter, cache_key, evict
from jsonreshape.reader import Document
from jsonreshape.recordset import filter_records, sort_records
from jsonreshape.search import SearchOptions, search
from jsonreshape.settings import Settings
from jsonreshape.tasks import Progress, format_bytes
from jsonreshape.writer import ExportOptions, export

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows-specific behaviour")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    yield name
    reader.set_scanner_backend(old)


def line(i: int, src: str = "old", pad: int = 30) -> bytes:
    return codec.dumps({"id": i, "src": src, "v": ("x" if src == "old" else "y") * pad}) + b"\n"


def jsonl_file(tmp_path: Path, n: int, name: str = "app.log.jsonl", pad: int = 30) -> tuple[Path, bytes]:
    data = b"".join(line(i, "old", pad) for i in range(n))
    p = tmp_path / name
    p.write_bytes(data)
    return p, data


def append(p: Path, data: bytes) -> None:
    with open(p, "ab") as f:
        f.write(data)


def array_file(tmp_path: Path, n: int, name: str = "recs.json", pad: int = 20) -> tuple[Path, bytes]:
    data = codec.dumps([{"id": i, "name": "x" * pad} for i in range(n)])
    p = tmp_path / name
    p.write_bytes(data)
    return p, data


def set_bytes(p: Path, pos: int, new: bytes, *, keep_mtime: bool = False) -> None:
    st = p.stat()
    with open(p, "r+b") as f:
        f.seek(pos)
        f.write(new)
    if keep_mtime:
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))


def merge_two_records(data: bytes, at: int) -> tuple[int, bytes]:
    """The first record of an array file starting at or after ``at`` merged with the next
    one into one record of the same total length (the reviewers' same-size edit)."""
    pos = data.index(b'{"id":', at)
    end = data.index(b"}", pos) + 1
    nxt_end = data.index(b"}", end + 1) + 1
    old = data[pos:nxt_end]
    head = old[:old.index(b'"name":"') + len(b'"name":"')]
    new = head + b"y" * (len(old) - len(head) - 2) + b'"}'
    assert len(new) == len(old) and new != old
    return pos, new


class Appender:
    """Another program appending whole JSON Lines lines to ``path`` (kept open, one write
    per line) while the ``with`` block runs."""

    def __init__(self, path: Path, pause: float = 0.001) -> None:
        self.path = path
        self.pause = pause
        self.lines = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with open_shared(self.path, "ab", buffering=0) as f:
            n = 10 ** 6
            while not self._stop.is_set():
                f.write(line(n, "new"))
                n += 1
                self.lines += 1
                time.sleep(self.pause)

    def __enter__(self) -> Appender:
        self._thread.start()
        deadline = time.monotonic() + 10
        while self.lines < 3:   # it is running
            assert time.monotonic() < deadline
            time.sleep(0.001)
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(10)


def snapshot_records(p: Path, size: int) -> list[Any]:
    """The records of the first ``size`` bytes of the JSON Lines file ``p``."""
    with open(p, "rb") as f:
        data = f.read(size)
    out = []
    for raw in data.split(b"\n"):
        if raw.strip():
            try:
                out.append(json.loads(raw))
            except ValueError:
                pass
    return out


# ==================================================================================
# N05: a file that only grows is read as the snapshot opened
# ==================================================================================
def test_appended_lines_keep_the_snapshot(tmp_path: Path, backend: str, lang_en) -> None:
    p, data = jsonl_file(tmp_path, 3000)
    with Document.open(p) as doc:
        doc.build_index()
        assert doc.appended_bytes == 0 and "reader.warn.appended" not in doc.info.warnings
        more = b"".join(line(i, "new") for i in range(5))
        append(p, more)
        assert not doc.check_changed()
        doc.ensure_unchanged()
        assert doc.appended_bytes == len(more)
        assert doc.record_count == 3000 and doc.data_size == len(data)
        assert [v for _, v in doc.iter_records()] == [json.loads(x) for x in data.splitlines()]
        assert [i for i, _ in doc.iter_records(list(range(2999, -1, -3)))] == list(range(2999, -1, -3))
        assert doc.record(2999)["id"] == 2999
        with doc.open_data() as f:
            assert f.read(10) == data[:10]
        assert doc.info.warnings.count("reader.warn.appended") == 1
        assert f"The file grew by {format_bytes(len(more))} after it was opened" in doc.info.warning_texts()[-1]
        # it keeps growing: one warning, its size follows
        append(p, line(7, "new") * 100)
        doc.ensure_unchanged()
        total = len(more) + 100 * len(line(7, "new"))
        assert doc.appended_bytes == total and doc.info.warnings.count("reader.warn.appended") == 1
        assert format_bytes(total) in doc.info.warning_texts()[-1]
        assert [i for i, _ in doc.iter_records()] == list(range(3000))
    # the next open reads the longer file, nothing appended after it
    with Document.open(p) as doc:
        doc.build_index()
        assert doc.record_count == 3105 and doc.appended_bytes == 0 and not doc.check_changed()


def test_growth_while_indexing_and_operations_run(tmp_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch,
                                                  lang_en) -> None:
    # every handle check hashes the original range again (no rate limit) and runs often:
    # the build's handle must keep its position, every operation must see the snapshot
    monkeypatch.setattr(reader, "GROWTH_RECHECK_SECONDS", 0.0)
    monkeypatch.setattr(reader, "VERIFY_BUILD_CHUNKS", 1)
    monkeypatch.setattr(reader, "VERIFY_READS", 8)
    n = 60_000
    p, _ = jsonl_file(tmp_path, n)   # ~4 MB: head, sampled pages and tail are hashed
    assert p.stat().st_size > 2 * HEAD_HASH_BYTES + SAMPLE_BLOCKS * SAMPLE_BYTES
    out = tmp_path / "out"
    out.mkdir()
    with Appender(p) as app:
        with Document.open(p) as doc:
            doc.build_index()
            expected = snapshot_records(p, doc.data_size)
            k = len(expected) - n
            # (a line the writer had not finished at the open would be a broken last line)
            assert doc.record_count == len(expected) and k >= 0 and doc.broken_count <= 1
            rs = filter_records(doc, "src == 'old'")
            assert rs.report.matched == n
            rs.close()
            rs = filter_records(doc, "src == 'new'")
            assert rs.report.matched == k
            rs.close()
            res = search(doc, SearchOptions.build("yyyyyyyy"), max_hits=None)
            assert res.record_count == k
            res.close()
            rs = sort_records(doc, ["-id"])
            order = rs.to_list()
            rs.close()
            assert len(order) == len(expected) and [expected[i]["id"] for i in order[:3]] == \
                sorted((r["id"] for r in expected), reverse=True)[:3]
            rep = export(doc, out / "all.jsonl", ExportOptions("jsonl"))
            assert rep.records == len(expected)
            assert [json.loads(x) for x in (out / "all.jsonl").read_bytes().splitlines()] == expected
            rep = export(doc, out / "sorted.csv", ExportOptions("csv", columns=["id", "src"]), order)
            assert rep.records == len(expected)
            doc.ensure_unchanged()
            assert app.lines > 0 and doc.appended_bytes > 0
            assert doc.info.warnings.count("reader.warn.appended") == 1
            # the installed index describes the version opened, not this session's growth
            with IndexReader.open(doc.index_path, touch=False) as ir:
                assert "reader.warn.appended" not in ir.meta["format"]["warnings"]
                assert ir.meta["data_size"] == doc.data_size


def test_growth_during_the_build_is_seen_by_its_handle_checks(tmp_path: Path, backend: str,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "VERIFY_BUILD_CHUNKS", 1)
    p, data = jsonl_file(tmp_path, 40_000)
    grown: list[int] = []

    def grow(info: Any) -> None:
        if not grown and info.done:
            grown.append(1)
            append(p, line(1, "new") * 10)

    with Document.open(p) as doc:
        doc.build_index(progress=Progress(callback=grow, min_interval=0))
        assert grown and doc.index_complete and doc.record_count == 40_000
        assert doc.appended_bytes == 10 * len(line(1, "new"))


@pytest.mark.parametrize("change", ["truncated", "same_size_head", "same_size_touched", "longer_head",
                                    "longer_tail", "longer_sampled", "replaced_longer"])
def test_changes_other_than_appending_still_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  change: str) -> None:
    monkeypatch.setattr(reader, "GROWTH_RECHECK_SECONDS", 0.0)
    p, data = array_file(tmp_path, 120_000)   # > 2 MiB: head, sampled pages and tail
    size = len(data)
    ranges = sample_ranges(size)
    assert len(ranges) == SAMPLE_BLOCKS
    with Document.open(p) as doc:
        doc.build_index()
        assert doc.record_count == 120_000
        assert doc.record(0) == {"id": 0, "name": "x" * 20}   # this thread's handle is open
        if change == "truncated":
            with open(p, "r+b") as f:
                f.truncate(size - 10)
        elif change == "same_size_head":
            set_bytes(p, 100, b"y", keep_mtime=True)
        elif change == "same_size_touched":
            st = p.stat()
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 3_000_000_000))
        elif change == "replaced_longer":
            os.remove(p)
            p.write_bytes(data[:50].replace(b'"x', b'"z', 1) + data[50:] + b" " * 100)
        else:   # an edit in the version opened, then the file grows
            pos = {"longer_head": 1000, "longer_tail": size - TAIL_HASH_BYTES + 100,
                   "longer_sampled": ranges[5][0] + 10}[change]
            assert data[pos:pos + 1] != b" "
            set_bytes(p, pos, b" ")
            append(p, b" " * 100)
        assert doc.check_changed()
        with pytest.raises(FileChangedError):
            doc.ensure_unchanged()
        if change != "same_size_head":    # (size and time unchanged: invisible to fstat)
            with pytest.raises(FileChangedError):
                doc.open_data()           # a new handle
        if change not in ("same_size_head", "replaced_longer"):   # (the old handle reads the old file)
            with pytest.raises(FileChangedError):
                list(doc.iter_blocks())   # this thread's handle
        assert doc.appended_bytes == 0 and "reader.warn.appended" not in doc.info.warnings


def test_handle_checks_of_a_growing_file_hash_again_after_the_interval(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    p, data = array_file(tmp_path, 120_000)
    with Document.open(p) as doc:
        doc.build_index()
        append(p, b" " * 50)
        monkeypatch.setattr(reader, "GROWTH_RECHECK_SECONDS", 3600.0)
        assert len(list(doc.iter_blocks())) >= 1          # verified once: append-only
        set_bytes(p, 500, b" ")                           # now rewritten, still longer
        append(p, b" " * 50)
        # within the interval a longer file is taken for growth by the cheap handle check
        # (the span checks still look at the records) -- the final check always hashes:
        with pytest.raises(FileChangedError):
            doc.ensure_unchanged()
        monkeypatch.setattr(reader, "GROWTH_RECHECK_SECONDS", 0.0)
        with pytest.raises(FileChangedError):
            list(doc.iter_blocks())


def test_small_file_growth_and_identity_helpers(tmp_path: Path) -> None:
    p = tmp_path / "small.jsonl"
    p.write_bytes(b'{"a": 1}\n{"a": 2}\n')
    ident = file_identity(p)
    assert ident.tail_hash == "" and ident.sample_hash == ""
    assert appended_bytes(p, ident) == 0
    append(p, b'{"a": 3}\n')
    assert appended_bytes(p, ident) == 9
    with open_shared(p, "rb", buffering=0) as f:
        f.seek(5)
        assert grown_from(f, ident) and f.tell() == 5     # position kept
    other = FileIdentity.from_dict(dict(ident.to_dict(), path=ident.path + ".other"))
    assert appended_bytes(p, other) is None               # another file
    st = p.stat()
    p.write_bytes(b'{"a": 9}\n{"a": 2}\n{"a": 3}\n')      # rewritten: the original range differs
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert appended_bytes(p, ident) is None
    with open_shared(p, "rb", buffering=0) as f:
        assert not grown_from(f, ident)
    p.write_bytes(b'{"a": 1}\n')                           # shorter
    assert appended_bytes(p, ident) is None
    missing = tmp_path / "missing.jsonl"
    with pytest.raises(JsonReshapeError) as info:
        appended_bytes(missing, FileIdentity.from_dict(dict(ident.to_dict(), path=fileio.normalize_path(missing))))
    assert info.value.key == "error.file_not_found"


def test_gzip_source_that_grows(tmp_path: Path, lang_en) -> None:
    first = b"".join(line(i) for i in range(2000))
    p = tmp_path / "log.jsonl.gz"
    p.write_bytes(gzip.compress(first))
    with Document.open(p) as doc:
        doc.build_index()
        member = gzip.compress(b"".join(line(i, "new") for i in range(10)))
        append(p, member)                       # gzip -c new.jsonl >> log.jsonl.gz
        doc.ensure_unchanged()
        assert doc.appended_bytes == len(member) and doc.record_count == 2000
        assert [i for i, _ in doc.iter_records()] == list(range(2000))
        assert "reader.warn.appended" in doc.info.warnings
    with Document.open(p) as doc:               # the next open has both members
        doc.build_index()
        assert doc.record_count == 2010


def test_prepared_copy_holds_the_identified_version_only(tmp_path: Path) -> None:
    first = b"".join(line(i, pad=300) for i in range(8000))   # > 1 MiB decompressed: several chunks
    p = tmp_path / "log.jsonl.gz"
    p.write_bytes(gzip.compress(first))
    appended: list[int] = []

    def grow(info: Any) -> None:
        if not appended and info.done:
            member = gzip.compress(b"".join(line(i, "new") for i in range(50)))
            append(p, member)                   # another program appends while the copy is made
            appended.append(len(member))

    pf = prepare_data_file(p, progress=Progress(callback=grow, min_interval=0))
    assert appended
    assert Path(pf.data_path).read_bytes() == first
    with Document.open(p) as doc:               # a new identity: a new copy with both members
        doc.build_index()
        assert doc.record_count == 8050


def test_appended_warning_texts(lang_en) -> None:
    one = t("reader.warn.appended", bytes=format_bytes(1))
    assert one.startswith("The file grew by 1 B after it was opened") and "not included" in one
    assert "12.3 MB" in t("reader.warn.appended", bytes=format_bytes(12_345_678))
    entry = MESSAGES["reader.warn.appended"]
    assert "{bytes}" in entry["uz"] and "{bytes}" in entry["en"]


_WRITER = r"""
import json, os, sys, time
path, stop = sys.argv[1], sys.argv[2]
n = 10 ** 6
with open(path, "ab", buffering=0) as f:      # kept open like a logging.FileHandler
    while not os.path.exists(stop):
        f.write((json.dumps({"id": n, "src": "new", "v": "y" * 30}, separators=(",", ":")) + "\n").encode())
        n += 1
        time.sleep(0.002)
"""


def test_cli_filter_and_sort_while_another_process_appends(tmp_path: Path) -> None:
    n = 50_000
    p, data = jsonl_file(tmp_path, n)
    stop = tmp_path / "stop.flag"
    home = tmp_path / "home"
    env = dict(os.environ, JSONRESHAPE_HOME=str(home), PYTHONIOENCODING="utf-8")
    writer = subprocess.Popen([sys.executable, "-c", _WRITER, str(p), str(stop)], cwd=str(tmp_path))
    try:
        deadline = time.monotonic() + 30
        while p.stat().st_size <= len(data):    # it is appending
            assert time.monotonic() < deadline and writer.poll() is None
            time.sleep(0.01)
        runs = {}
        for name, args in (("filter", ["filter", str(p), str(tmp_path / "old.jsonl"), "--where", "src == 'old'"]),
                           ("sort", ["sort", str(p), str(tmp_path / "sorted.jsonl"), "--by", "id"])):
            runs[name] = subprocess.run([sys.executable, "-m", "jsonreshape", "--lang", "en", *args], cwd=str(ROOT),
                                        env=env, capture_output=True, timeout=600)
        grew = p.stat().st_size
    finally:
        stop.write_bytes(b"")
        writer.wait(30)
    for name, r in runs.items():
        text = (r.stdout + r.stderr).decode("utf-8", "replace")
        assert r.returncode == 0, (name, text)
    assert grew > len(data)
    old = [json.loads(x) for x in (tmp_path / "old.jsonl").read_bytes().splitlines()]
    assert len(old) == n and all(r["src"] == "old" for r in old) and old[-1]["id"] == n - 1
    rows = [json.loads(x) for x in (tmp_path / "sorted.jsonl").read_bytes().splitlines()]
    ids = [r["id"] for r in rows]
    assert len(rows) >= n and ids == sorted(ids) and ids[:n] == list(range(n))
    assert all(r["src"] == "new" for r in rows[n:])      # the new lines of its snapshot only


# ==================================================================================
# N08: protected and in-use cache files do not count against the budget
# ==================================================================================
def _gz_input(tmp_path: Path, k: int, n: int = 3000) -> Path:
    p = tmp_path / f"in{k}.json.gz"
    p.write_bytes(gzip.compress(codec.dumps([{"id": i, "k": k, "text": "x" * 200} for i in range(n)])))
    return p


def test_recent_prepared_copies_do_not_evict_the_index_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    s = Settings(cache_dir=str(cache), cache_limit_bytes=1_000_000)   # less than the copies, more than the indexes
    index_files, copies = [], []
    for k in range(4):
        with Document.open(_gz_input(tmp_path, k), settings=s) as doc:
            doc.build_index()
            index_files.append(doc.index_path)
            copies.append(Path(doc.data_path))
        time.sleep(0.02)
    assert sum(c.stat().st_size for c in copies) > s.cache_limit_bytes      # protected for an hour
    assert sum(f.stat().st_size for f in index_files) < s.cache_limit_bytes
    assert all(f.exists() for f in index_files)   # before: only the newest one was left
    # index files beyond the limit still go (least recently used first); the copies stay
    tiny = Settings(cache_dir=str(cache), cache_limit_bytes=max(f.stat().st_size for f in index_files) + 100)
    old = time.time() - 600
    for i, f in enumerate(index_files):
        os.utime(f, (old + i, old + i))
    with Document.open(_gz_input(tmp_path, 9), settings=tiny) as doc:
        doc.build_index()
        mine = doc.index_path
    assert mine.exists() and [f.exists() for f in index_files] == [False, False, False, True]
    assert all(c.exists() for c in copies)


def test_evict_counts_only_removable_entries(tmp_path: Path) -> None:
    idx = tmp_path / "index"
    pdir = tmp_path / "prepared"
    idx.mkdir()
    pdir.mkdir()
    sizes = []
    for k in range(3):
        f = idx / f"{k:064x}.jri"
        f.write_bytes(b"i" * 1000)
        t0 = time.time() - 1000 + k
        os.utime(f, (t0, t0))
        sizes.append(1000)
    for k in range(2):                       # two big copies used a minute ago: protected
        (pdir / f"c{k}.json").write_bytes(b"p" * 50_000)
        (pdir / f"c{k}.meta").write_bytes(b"{}")
        t0 = time.time() - 60
        os.utime(pdir / f"c{k}.meta", (t0, t0))
    assert evict(3000, directory=idx, prepared_dir=pdir) == 0
    assert len(os.listdir(idx)) == 3 and len(os.listdir(pdir)) == 4
    assert evict(2000, directory=idx, prepared_dir=pdir) == 1000           # the oldest index file only
    assert sorted(os.listdir(idx)) == [f"{1:064x}.jri", f"{2:064x}.jri"] and len(os.listdir(pdir)) == 4
    # unprotected (old) copies are evicted like any other file, least recently used first
    for k in range(2):
        t0 = time.time() - 7200 - k
        os.utime(pdir / f"c{k}.meta", (t0, t0))
    assert evict(2000, directory=idx, prepared_dir=pdir) == 100_000
    assert len(os.listdir(idx)) == 2 and os.listdir(pdir) == []


def _aged_index(directory: Path, name: str, n: int, age: float) -> Path:
    p = directory / name
    w = IndexWriter(p)
    w.append(array("Q", [v for k in range(n) for v in (10 * k, 10 * k + 5)]).tobytes())
    w.finalize({"kind": "test"}).close()
    t0 = time.time() - age
    os.utime(p, (t0, t0))
    return p


@windows_only
def test_index_files_in_use_do_not_count_against_the_budget(tmp_path: Path) -> None:
    a = _aged_index(tmp_path, "a.jri", 50, 500)
    b = _aged_index(tmp_path, "b.jri", 50, 400)
    c = _aged_index(tmp_path, "c.jri", 50, 300)
    r = IndexReader.open(a, touch=False)     # mapped: cannot be removed
    assert r is not None
    try:
        evict(a.stat().st_size, directory=tmp_path)
        assert a.exists() and not b.exists() and c.exists()   # before: c went as well
    finally:
        r.close()


def test_prune_prepared_does_not_count_protected_copies(tmp_path: Path) -> None:
    sources = []
    for i in range(3):
        src = tmp_path / f"s{i}.json.gz"
        src.write_bytes(gzip.compress(b"[" + b"1," * 3000 + b"1]"))
        sources.append(prepare_data_file(src))
    now = time.time()
    for age, pf in zip((300, 200, 100), sources):
        meta = Path(pf.data_path).with_suffix(".meta")
        os.utime(meta, (now - age, now - age))
    size = os.path.getsize(sources[2].data_path)
    # s0 is kept (in use): only s1 and s2 count, and s1 (older) is enough to fit
    assert prune_prepared(size, min_age=0, keep=[sources[0].data_path]) == size
    assert [Path(pf.data_path).exists() for pf in sources] == [True, False, True]


# ==================================================================================
# robustness F8: sampled pages in the identity
# ==================================================================================
def test_sample_ranges() -> None:
    assert sample_ranges(0) == [] and sample_ranges(2 * HEAD_HASH_BYTES) == []
    assert sample_ranges(HEAD_HASH_BYTES + TAIL_HASH_BYTES + 100) == [(HEAD_HASH_BYTES, HEAD_HASH_BYTES + 100)]
    small = HEAD_HASH_BYTES + TAIL_HASH_BYTES + SAMPLE_BLOCKS * SAMPLE_BYTES
    assert sample_ranges(small) == [(HEAD_HASH_BYTES, HEAD_HASH_BYTES + SAMPLE_BLOCKS * SAMPLE_BYTES)]
    for size in (small + 1, 13_088_891, 5 * 10 ** 9 + 7):
        r = sample_ranges(size)
        assert r == sample_ranges(size) and len(r) == SAMPLE_BLOCKS
        assert all(e - s == SAMPLE_BYTES for s, e in r)
        assert r[0][0] >= HEAD_HASH_BYTES and r[-1][1] <= size - TAIL_HASH_BYTES
        assert all(r[k][1] <= r[k + 1][0] for k in range(len(r) - 1))
        assert all(s % SAMPLE_BYTES == 0 for s, _ in r[:-1])
    big = sample_ranges(10 ** 9)
    gaps = [big[k + 1][0] - big[k][0] for k in range(len(big) - 1)]
    assert max(gaps) - min(gaps) <= SAMPLE_BYTES      # spread evenly


def test_identity_sample_hash_is_part_of_matches_digest_and_cache_key(tmp_path: Path) -> None:
    p, data = array_file(tmp_path, 120_000)
    a = file_identity(p)
    r = sample_ranges(len(data))
    want = hashlib.sha256(b"".join(data[s:e] for s, e in r)).hexdigest()
    assert a.sample_hash == want and a.tail_hash and a.head_hash
    assert FileIdentity.from_dict(json.loads(json.dumps(a.to_dict()))) == a
    b = FileIdentity.from_dict(dict(a.to_dict(), sample_hash="0" * 64))
    assert not a.matches(b) and a.digest() != b.digest()
    assert cache_key(a, "array", None) != cache_key(b, "array", None)
    legacy = {k: v for k, v in a.to_dict().items() if k != "sample_hash"}   # written before the field existed
    assert not FileIdentity.from_dict(legacy).matches(a)
    small = tmp_path / "small.json"
    small.write_bytes(b"[1, 2, 3]")
    s = file_identity(small)
    assert s.sample_hash == "" and FileIdentity.from_dict({k: v for k, v in s.to_dict().items()
                                                            if k != "sample_hash"}).matches(s)


def test_same_size_edit_in_a_sampled_page_invalidates_the_cached_index(tmp_path: Path) -> None:
    # the reviewers' case: two records in the middle merged into one of the same length,
    # the modification time restored -- now caught when the edit touches a sampled page
    p, data = array_file(tmp_path, 120_000)
    r = sample_ranges(len(data))
    pos, new = merge_two_records(data, r[7][0])
    assert r[7][0] <= pos < r[7][1]
    assert pos > HEAD_HASH_BYTES and pos + len(new) < len(data) - TAIL_HASH_BYTES
    with Document.open(p) as doc:
        doc.build_index()
        n = doc.record_count
        key = doc._main_key()
        set_bytes(p, pos, new, keep_mtime=True)
        assert p.stat().st_size == len(data)
        assert doc.check_changed()
        with pytest.raises(FileChangedError):
            doc.ensure_unchanged()
    with Document.open(p) as doc:
        assert doc._main_key() != key and not doc.index_complete   # the stale index is not used
        doc.build_index()
        assert doc.record_count == n - 1
        assert all(v["name"].startswith(("x", "y")) for _, v in doc.iter_records())
