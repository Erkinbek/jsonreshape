"""jsonreshape.index: binary span index files (docs/ARCHITECTURE.md §5.2, AGENT_PROMPT §3
"Indeks": binary, mmap-read, never loaded into RAM, cached by identity, LRU eviction).

* round trip of spans + metadata, exact header layout;
* live reads (count/get/get_many) from another thread while the writer appends;
* corrupt / incomplete / wrong-version / truncated files are ignored (``None``), never a crash;
* cache keys; LRU eviction (mtime), skipping files in use and ``keep``;
* Windows: a final file that is memory-mapped cannot be replaced -> unique-name fallback.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import threading
import time
from array import array
from pathlib import Path

import pytest

from jsonreshape import index as ix
from jsonreshape.fileio import FileIdentity
from jsonreshape.index import (
    FLAG_COMPLETE,
    FLAG_ERROR,
    FLAG_MEMBERS,
    HEADER_SIZE,
    INDEX_VERSION,
    MAGIC,
    IndexReader,
    IndexWriter,
    cache_key,
    evict,
    find_index,
    index_path,
)

WINDOWS = sys.platform == "win32"
HEADER = struct.Struct("<8sIIQQQ")


def pairs(*vals: int) -> bytes:
    return array("Q", vals).tobytes()


def make_spans(n: int, start: int = 0) -> list[tuple[int, int]]:
    return [(start + 10 * i, start + 10 * i + 7) for i in range(n)]


def spans_bytes(spans: list[tuple[int, int]]) -> bytes:
    a = array("Q")
    for s, e in spans:
        a.append(s)
        a.append(e)
    return a.tobytes()


def build(path: Path, spans: list[tuple[int, int]], meta: dict | None = None, flags: int = 0,
          **kw) -> IndexReader:
    w = IndexWriter(path, **kw)
    data = spans_bytes(spans)
    step = 16 * 7
    for i in range(0, len(data), step):
        w.append(data[i:i + step])
    return w.finalize(meta or {"kind": "test"}, flags)


# ----------------------------------------------------------------------------------
# round trip and layout
# ----------------------------------------------------------------------------------
def test_round_trip(tmp_path: Path) -> None:
    spans = make_spans(1000, start=3)
    meta = {"kind": "test", "text": "Toshkent \u0422\u043e\u0448\u043a\u0435\u043d\u0442", "list": [1, 2]}
    final = tmp_path / "a.jri"
    reader = build(final, spans, meta, FLAG_ERROR | FLAG_MEMBERS)
    try:
        assert reader.path == final and final.exists()
        assert reader.count == len(reader) == 1000
        assert reader.flags == FLAG_COMPLETE | FLAG_ERROR | FLAG_MEMBERS
        assert reader.meta["text"] == meta["text"] and reader.meta["list"] == [1, 2]
        assert reader.meta["index_version"] == INDEX_VERSION and reader.meta["count"] == 1000
        assert reader.get(0) == spans[0] and reader.get(999) == spans[999]
        assert reader.get_many(10, 3) == spans_bytes(spans[10:13])
        assert reader.get_many(998, 10) == spans_bytes(spans[998:])  # clipped
        assert reader.get_many(1000, 5) == b""
        assert list(reader.spans_array(5, 2)) == [*spans[5], *spans[6]]
        assert reader.read_meta() == reader.meta
        with pytest.raises(IndexError):
            reader.get(1000)
        with pytest.raises(IndexError):
            reader.get(-1)
    finally:
        reader.close()
    reader.close()  # idempotent
    assert reader.closed
    with pytest.raises(ValueError):
        reader.get(0)
    again = IndexReader.open(final)
    assert again is not None and again.count == 1000 and again.get(500) == spans[500]
    again.close()


def test_read_many_equals_get_many(tmp_path: Path) -> None:
    spans = make_spans(5000)
    r = build(tmp_path / "rm.jri", spans)
    try:
        for i, n in ((0, 5000), (17, 300), (4990, 100), (5000, 3), (0, 0)):
            assert r.read_many(i, n) == r.get_many(i, n)
        with pytest.raises(IndexError):
            r.read_many(-1, 2)
    finally:
        r.close()
    with pytest.raises(ValueError):
        r.read_many(0, 1)
    (tmp_path / "rm.jri").unlink()  # the extra read handle was closed too


def test_header_layout(tmp_path: Path) -> None:
    final = tmp_path / "h.jri"
    build(final, make_spans(5), {"kind": "test"}, FLAG_MEMBERS).close()
    raw = final.read_bytes()
    magic, version, flags, count, meta_offset, meta_length = HEADER.unpack(raw[:HEADER.size])
    assert magic == MAGIC == b"JRIDX\x00\x01\x00"
    assert version == INDEX_VERSION == 1
    assert flags == FLAG_COMPLETE | FLAG_MEMBERS
    assert count == 5
    assert meta_offset == HEADER_SIZE + 16 * 5 == 4096 + 80
    assert raw[HEADER.size:HEADER_SIZE] == bytes(HEADER_SIZE - HEADER.size)  # reserved zeros
    assert raw[HEADER_SIZE:meta_offset] == spans_bytes(make_spans(5))
    assert meta_offset + meta_length == len(raw)
    meta = json.loads(raw[meta_offset:].decode("utf-8"))
    assert meta["kind"] == "test" and meta["count"] == 5


def test_empty_index(tmp_path: Path) -> None:
    r = build(tmp_path / "e.jri", [], {"kind": "test"})
    assert r.count == 0 and r.get_many(0, 10) == b"" and len(r.spans_array()) == 0
    r.close()


def test_meta_keeps_lone_surrogates_and_unicode(tmp_path: Path) -> None:
    meta = {"kind": "test", "path": ["\ud800x", "\U0001f600", "O\u02bbzbekiston"]}
    r = build(tmp_path / "s.jri", make_spans(2), meta)
    try:
        assert r.meta["path"] == meta["path"]
    finally:
        r.close()


def test_append_validation(tmp_path: Path) -> None:
    w = IndexWriter(tmp_path / "v.jri")
    with pytest.raises(ValueError):
        w.append(b"x" * 15)
    with pytest.raises(TypeError):
        w.append("0123456789abcdef")  # type: ignore[arg-type]
    w.append(b"")
    w.append(bytearray(pairs(1, 2)))
    w.append(memoryview(pairs(3, 4)))
    assert w.count == 2 and w.get(1) == (3, 4)
    r = w.finalize({"kind": "test"})
    with pytest.raises(ValueError):
        w.append(pairs(5, 6))
    with pytest.raises(ValueError):
        w.finalize({"kind": "test"})
    assert w.state == "final" and w.used_path == r.path
    r.close()


def test_many_spans_flush_cycles(tmp_path: Path) -> None:
    spans = make_spans(200_000)
    r = build(tmp_path / "m.jri", spans, flush_bytes=4096)
    try:
        assert r.count == 200_000
        a = r.spans_array()
        assert a[0::2][123456] == spans[123456][0] and a[1::2][-1] == spans[-1][1]
    finally:
        r.close()


# ----------------------------------------------------------------------------------
# live reads while writing
# ----------------------------------------------------------------------------------
def test_live_reads_cross_flush_boundary(tmp_path: Path) -> None:
    w = IndexWriter(tmp_path / "live.jri", flush_bytes=16 * 4)
    spans = make_spans(10)
    for i, (s, e) in enumerate(spans):
        w.append(pairs(s, e))
        assert w.count == i + 1
        assert w.get(i) == (s, e)
        assert w.get(0) == spans[0]
        assert w.get_many(0, i + 1) == spans_bytes(spans[:i + 1])
    # straddles flushed (file) and unflushed (memory) spans
    assert w.get_many(2, 7) == spans_bytes(spans[2:9])
    with pytest.raises(IndexError):
        w.get(10)
    r = w.finalize({"kind": "test"})
    # after finalize the writer delegates to the reader
    assert w.count == 10 and w.get(9) == spans[9] and w.get_many(0, 10) == spans_bytes(spans)
    r.close()


def test_live_reads_from_another_thread(tmp_path: Path) -> None:
    n = 50_000
    spans = make_spans(n)
    w = IndexWriter(tmp_path / "thread.jri", flush_bytes=16 * 1000)
    errors: list[str] = []
    seen_counts: list[int] = []
    stop = threading.Event()

    def reader_thread() -> None:
        try:
            while not stop.is_set():
                c = w.count
                seen_counts.append(c)
                if c:
                    i = (c * 7919) % c
                    if w.get(i) != spans[i]:
                        errors.append(f"get({i})")
                    lo = max(0, c - 1500)
                    if w.get_many(lo, c - lo) != spans_bytes(spans[lo:c]):
                        errors.append(f"get_many({lo}, {c - lo})")
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(repr(exc))

    th = threading.Thread(target=reader_thread)
    th.start()
    data = spans_bytes(spans)
    for i in range(0, len(data), 16 * 37):
        w.append(data[i:i + 16 * 37])
        if i % (16 * 37 * 50) == 0:
            time.sleep(0.001)
    r = w.finalize({"kind": "test"})
    time.sleep(0.02)
    stop.set()
    th.join()
    try:
        assert not errors, errors[:5]
        assert len(set(seen_counts)) > 3, "the reader should have seen the index grow"
        assert r.count == n
    finally:
        r.close()


# ----------------------------------------------------------------------------------
# abort / temp files
# ----------------------------------------------------------------------------------
def test_abort_removes_temp_file(tmp_path: Path) -> None:
    w = IndexWriter(tmp_path / "x.jri", flush_bytes=32)
    w.append(spans_bytes(make_spans(100)))
    assert w.path.exists() and w.path.name.startswith(".") and w.path.suffix == ".tmp"
    w.abort()
    w.abort()  # idempotent
    assert not w.path.exists() and not (tmp_path / "x.jri").exists()
    assert w.state == "aborted" and w.count == 0
    with pytest.raises(ValueError):
        w.append(pairs(1, 2))
    assert os.listdir(tmp_path) == []


def test_context_manager_aborts_on_error(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        with IndexWriter(tmp_path / "c.jri") as w:
            w.append(pairs(1, 2))
            raise RuntimeError("boom")
    assert os.listdir(tmp_path) == []


def test_finalize_replaces_an_unused_older_index(tmp_path: Path) -> None:
    final = tmp_path / "k.jri"
    build(final, make_spans(3)).close()
    r = build(final, make_spans(5))
    try:
        assert r.path == final and r.count == 5
        assert sorted(os.listdir(tmp_path)) == ["k.jri"]
    finally:
        r.close()


# ----------------------------------------------------------------------------------
# corrupt files
# ----------------------------------------------------------------------------------
def _valid_bytes(tmp_path: Path) -> bytes:
    p = tmp_path / "valid.jri"
    build(p, make_spans(4), {"kind": "test"}).close()
    data = p.read_bytes()
    p.unlink()
    return data


def _patch_header(data: bytes, **fields: int | bytes) -> bytes:
    names = ("magic", "version", "flags", "count", "meta_offset", "meta_length")
    vals = dict(zip(names, HEADER.unpack(data[:HEADER.size])))
    vals.update(fields)
    return HEADER.pack(*(vals[n] for n in names)) + data[HEADER.size:]


@pytest.mark.parametrize("case", [
    "missing", "empty", "short", "garbage", "magic", "version", "incomplete", "count_too_big",
    "meta_offset", "meta_length_beyond_eof", "meta_not_json", "meta_not_object", "meta_wrong_version",
    "truncated_spans",
])
def test_corrupt_or_incomplete_files_are_ignored(tmp_path: Path, case: str) -> None:
    good = _valid_bytes(tmp_path)
    p = tmp_path / "bad.jri"
    meta_offset = HEADER_SIZE + 16 * 4
    if case == "missing":
        pass
    elif case == "empty":
        p.write_bytes(b"")
    elif case == "short":
        p.write_bytes(good[:100])
    elif case == "garbage":
        p.write_bytes(os.urandom(5000))
    elif case == "magic":
        p.write_bytes(_patch_header(good, magic=b"JRIDX\x00\x02\x00"))
    elif case == "version":
        p.write_bytes(_patch_header(good, version=2))
    elif case == "incomplete":
        p.write_bytes(_patch_header(good, flags=0))
    elif case == "count_too_big":
        p.write_bytes(_patch_header(good, count=10**12))
    elif case == "meta_offset":
        p.write_bytes(_patch_header(good, meta_offset=meta_offset + 16))
    elif case == "meta_length_beyond_eof":
        p.write_bytes(_patch_header(good, meta_length=10**6))
    elif case == "meta_not_json":
        p.write_bytes(good[:meta_offset] + b"{not json" + b" " * (len(good) - meta_offset - 9))
    elif case == "meta_not_object":
        body = b"[1,2,3]"
        p.write_bytes(_patch_header(good[:meta_offset] + body, meta_length=len(body)))
    elif case == "meta_wrong_version":
        body = json.dumps({"index_version": 99}).encode()
        p.write_bytes(_patch_header(good[:meta_offset] + body, meta_length=len(body)))
    elif case == "truncated_spans":
        p.write_bytes(good[:HEADER_SIZE + 20])
    assert IndexReader.open(p) is None


def test_find_index_skips_corrupt_canonical_and_uses_alternate(tmp_path: Path) -> None:
    key = "ab" * 32
    canonical = index_path(key, tmp_path)
    canonical.write_bytes(b"garbage")
    assert find_index(key, tmp_path) is None
    alt = tmp_path / f"{key}~0123abcd.jri"
    build(alt, make_spans(2)).close()
    r = find_index(key, tmp_path)
    assert r is not None and r.path == alt and r.count == 2
    r.close()


# ----------------------------------------------------------------------------------
# cache keys
# ----------------------------------------------------------------------------------
def test_cache_key() -> None:
    ident = FileIdentity(path="c:\\data\\a.json", size=100, mtime_ns=123, head_hash="ff" * 32)
    k = cache_key(ident, "array", None)
    assert len(k) == 64 and int(k, 16) >= 0
    assert k == cache_key(ident, "array", None)  # stable
    variants = {
        cache_key(FileIdentity(ident.path, 101, 123, ident.head_hash), "array", None),
        cache_key(FileIdentity(ident.path, 100, 124, ident.head_hash), "array", None),
        cache_key(FileIdentity(ident.path, 100, 123, "ee" * 32), "array", None),
        cache_key(FileIdentity("c:\\data\\b.json", 100, 123, ident.head_hash), "array", None),
        cache_key(ident, "jsonl", None),
        cache_key(ident, "wrapped", ("data",)),
        cache_key(ident, "wrapped", ("data", 0)),
        cache_key(ident, "wrapped", ("data", "0")),
        cache_key(ident, "array", None, "node:5"),
        cache_key(ident, "array", None, "node:6"),
        cache_key(ident, "wrapped", ("\ud800",)),
    }
    assert k not in variants and len(variants) == 11


# ----------------------------------------------------------------------------------
# eviction
# ----------------------------------------------------------------------------------
def _aged_index(directory: Path, name: str, n: int, age: float) -> Path:
    p = directory / name
    build(p, make_spans(n)).close()
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def test_evict_lru(tmp_path: Path) -> None:
    old = _aged_index(tmp_path, "old.jri", 100, 300)
    mid = _aged_index(tmp_path, "mid.jri", 100, 200)
    new = _aged_index(tmp_path, "new.jri", 100, 100)
    other = tmp_path / "notes.txt"
    other.write_text("not an index")
    size = old.stat().st_size
    assert evict(10 * size, directory=tmp_path) == 0  # fits already
    freed = evict(2 * size, directory=tmp_path)
    assert freed == size and not old.exists() and mid.exists() and new.exists() and other.exists()
    # opening an index touches it: "mid" becomes the most recently used
    r = IndexReader.open(mid)
    assert r is not None
    r.close()
    assert evict(size, directory=tmp_path) == size
    assert mid.exists() and not new.exists()


def test_evict_respects_keep_and_removes_stale_temp_files(tmp_path: Path) -> None:
    a = _aged_index(tmp_path, "a.jri", 50, 500)
    b = _aged_index(tmp_path, "b.jri", 50, 400)
    stale = tmp_path / ".x.jri.123.abcd.tmp"
    stale.write_bytes(b"partial")
    t = time.time() - 2 * 86400
    os.utime(stale, (t, t))
    fresh = tmp_path / ".y.jri.123.abcd.tmp"
    fresh.write_bytes(b"partial")
    evict(0, keep=[a], directory=tmp_path)
    assert a.exists() and not b.exists()
    assert not stale.exists() and fresh.exists()


@pytest.mark.skipif(not WINDOWS, reason="Windows: memory-mapped files cannot be deleted")
def test_evict_skips_files_in_use(tmp_path: Path) -> None:
    a = _aged_index(tmp_path, "a.jri", 50, 500)
    b = _aged_index(tmp_path, "b.jri", 50, 400)
    r = IndexReader.open(a, touch=False)
    assert r is not None
    try:
        evict(0, directory=tmp_path)  # must not raise
        assert a.exists() and not b.exists()
        assert r.get(3) == make_spans(50)[3]
    finally:
        r.close()
    evict(0, directory=tmp_path)
    assert not a.exists()


def test_reader_open_touches_mtime(tmp_path: Path) -> None:
    p = _aged_index(tmp_path, "t.jri", 3, 1000)
    before = p.stat().st_mtime
    r = IndexReader.open(p)
    assert r is not None
    r.close()
    assert p.stat().st_mtime > before + 500
    p2 = _aged_index(tmp_path, "t2.jri", 3, 1000)
    r = IndexReader.open(p2, touch=False)
    assert r is not None
    r.close()
    assert p2.stat().st_mtime < time.time() - 500


# ----------------------------------------------------------------------------------
# Windows: replacing a memory-mapped index
# ----------------------------------------------------------------------------------
@pytest.mark.skipif(not WINDOWS, reason="Windows-specific: a mapped file cannot be replaced")
def test_replace_while_mapped_keeps_a_unique_file(tmp_path: Path) -> None:
    key = "cd" * 32
    final = index_path(key, tmp_path)
    first = build(final, make_spans(3), {"kind": "test", "n": 1})
    try:
        w = IndexWriter(final)
        w.append(spans_bytes(make_spans(7)))
        second = w.finalize({"kind": "test", "n": 2})
        try:
            assert w.unique_fallback and w.used_path == second.path != final
            assert second.path.name.startswith(key + "~") and second.path.suffix == ".jri"
            assert second.count == 7 and second.meta["n"] == 2
            assert first.count == 3 and first.get(2) == make_spans(3)[2]  # old mapping intact
            assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
            found = find_index(key, tmp_path)  # canonical name first
            assert found is not None and found.meta["n"] == 1
            found.close()
        finally:
            second.close()
    finally:
        first.close()
    final.unlink()
    found = find_index(key, tmp_path)  # alternate when the canonical file is gone
    assert found is not None and found.meta["n"] == 2
    found.close()


def test_reader_releases_file_on_close(tmp_path: Path) -> None:
    p = tmp_path / "r.jri"
    r = build(p, make_spans(10))
    assert r.get(1) == make_spans(10)[1]
    r.close()
    p.unlink()  # no lingering mapping/handle (Windows would refuse otherwise)
    assert not p.exists()


def test_default_directory_is_the_app_index_dir(tmp_path: Path) -> None:
    from jsonreshape.appdirs import index_dir

    assert ix.default_index_dir() == index_dir()
    assert index_path("ab").parent == index_dir()
    custom = tmp_path / "cache"
    assert ix.default_index_dir(custom) == custom and custom.is_dir()
