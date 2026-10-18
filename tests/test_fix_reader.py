"""Regression tests of the review findings fixed in the reader workstream (phase 5).

* change detection while reading (requirements F1, robustness F1/F8): new handles are
  checked against the identity, stale spans on a rewritten file raise FileChangedError,
  the identity hashes the last MiB too, a build never installs an index of a file that
  changed while it was scanned, ``check_changed`` retries a briefly locked source;
* format detection of JSON Lines / concatenated JSON whose first record is bigger than
  the head (blackbox F2, codereview CR-03, requirements F12) and ``Document.format_hint``;
* record paths ``/data/0`` and ``data.0`` (blackbox F11, codereview CR-01) and the
  ``arrays`` items kind of candidates (codereview CR-11);
* JSON error offsets of UTF-16 / gzip inputs (blackbox F15, codereview CR-13,
  requirements F15);
* sparse ascending block reads and the progress of ``iter_blocks`` (blackbox F5, F10);
* unusable app / cache folders, long paths and logging (blackbox F6, robustness F7/F12);
* several processes indexing one file (requirements F7, robustness F3/F14);
* prepared copies under the cache folder and one cache budget (blackbox F9,
  requirements F8, codereview CR-08);
* fileio messages (robustness F13) and a lone surrogate in a gzip file name (robustness F15).
"""

from __future__ import annotations

import codecs
import errno
import gzip
import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import appdirs, codec, fileio, reader
from jsonreshape import index as ix
from jsonreshape import log as jlog
from jsonreshape.errors import FileChangedError, IndexBuildError, JsonReshapeError, UserInputError
from jsonreshape.fileio import atomic_path, atomic_write, file_identity, prepare_data_file
from jsonreshape.i18n import t
from jsonreshape.index import BuildLock, IndexReader, IndexWriter, evict, index_path, lock_path
from jsonreshape.reader import Document, RecordPathCandidate, detect_format
from jsonreshape.settings import Settings
from jsonreshape.tasks import Progress

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows-specific behaviour")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    yield name
    reader.set_scanner_backend(old)


def write(tmp_path: Path, data: bytes, name: str = "doc.json") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def jri_files(directory: Path | None = None) -> list[str]:
    d = directory if directory is not None else appdirs.index_dir()
    return sorted(n for n in os.listdir(d) if n.endswith(".jri"))


def records_file(tmp_path: Path, n: int = 3000, pad: int = 20, name: str = "recs.json") -> tuple[Path, bytes]:
    data = codec.dumps([{"id": i, "name": "x" * pad} for i in range(n)])
    return write(tmp_path, data, name), data


def rewrite_keeping_size_and_mtime(p: Path, new: bytes) -> None:
    st = p.stat()
    assert len(new) == st.st_size
    with open(p, "r+b") as f:
        f.write(new)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))


# ==================================================================================
# change detection (requirements F1, robustness F1, robustness F8)
# ==================================================================================
def test_replaced_file_is_detected_by_every_new_handle(tmp_path: Path) -> None:
    p = write(tmp_path, b'[{"id": 1, "name": "old one"}, {"id": 2, "name": "old two"}]')
    with Document.open(p) as doc:
        doc.build_index()   # the build read through its own handle, now closed
        os.remove(p)        # another program: delete and write the new version
        p.write_bytes(b'[{"id":10,"name":"NEW"},{"id":20,"name":"NEW 2"},{"id":30,"name":"NEW 3"}]')
        assert doc.check_changed()
        with pytest.raises(FileChangedError):
            doc.record(0)
        with pytest.raises(FileChangedError):
            list(doc.iter_blocks())
        with pytest.raises(FileChangedError):
            list(doc.iter_blocks([1, 0]))
        with pytest.raises(FileChangedError):
            doc.open_data()
        with pytest.raises(FileChangedError) as info:
            doc.ensure_unchanged()
        assert info.value.params["path"] == str(p)


def test_replacement_seen_by_a_handle_opened_before_is_caught_at_the_end(tmp_path: Path) -> None:
    p = write(tmp_path, b'[{"id": 1}, {"id": 2}]')
    with Document.open(p) as doc:
        doc.build_index()
        assert doc.record(0) == {"id": 1}      # this thread's handle holds the old file
        os.remove(p)
        p.write_bytes(b'[{"id": 1}, {"id": 2}, {"id": 3}]')
        assert doc.record(1) == {"id": 2}      # still consistent: the old data
        with pytest.raises(FileChangedError):  # ... but the operation never reports success
            doc.ensure_unchanged()


@pytest.mark.parametrize("mapped", [True, False], ids=["map", "read"])
def test_same_size_rewrite_with_restored_mtime_is_caught_by_span_checks(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mapped: bool) -> None:
    monkeypatch.setattr(reader, "MAP_SCATTERED", mapped)
    p, data = records_file(tmp_path)
    shifted = b"[ " + data[1:-2] + b"]"     # every record one byte further, same size
    with Document.open(p) as doc:
        doc.build_index()
        n = doc.record_count
        rewrite_keeping_size_and_mtime(p, shifted)
        # size and mtime match: only the bytes at the spans tell
        with pytest.raises(FileChangedError):
            list(doc.iter_blocks())                               # contiguous blocks (sampled)
        with pytest.raises(FileChangedError):
            list(doc.iter_blocks(list(range(n - 1, -1, -7))))    # scattered blocks
        with pytest.raises(FileChangedError):
            doc.record_bytes(n // 2)
        assert doc.check_changed()


def test_same_size_edit_in_the_last_mib_changes_the_identity_and_the_cache_key(tmp_path: Path) -> None:
    # robustness F8: records 2..3 MB into the file are merged into one record of the
    # same length and the modification time is restored
    p, data = records_file(tmp_path, n=90_000)
    assert len(data) > 2 * fileio.HEAD_HASH_BYTES
    with Document.open(p) as doc:
        doc.build_index()
        n = doc.record_count
        key = doc._main_key()
    pos = data.index(b'{"id":88000,')
    end = data.index(b"}", data.index(b"}", pos) + 1) + 1
    old = data[pos:end]
    new = b'{"id":88000,"name":"' + b"y" * (len(old) - len(b'{"id":88000,"name":""}')) + b'"}'
    assert len(new) == len(old)
    rewrite_keeping_size_and_mtime(p, data[:pos] + new + data[end:])
    with Document.open(p) as doc:
        assert doc._main_key() != key and not doc.index_complete   # the stale index is not used
        doc.build_index()
        assert doc.record_count == n - 1 and doc.record(88000)["name"].startswith("yyy")
    a = fileio.FileIdentity.from_dict(file_identity(p).to_dict())
    assert a.tail_hash and a.matches(file_identity(p))
    assert not a.matches(fileio.FileIdentity(a.path, a.size, a.mtime_ns, a.head_hash, ""))


def test_change_during_the_build_installs_no_index(tmp_path: Path) -> None:
    p, data = records_file(tmp_path, n=40_000)   # ~1.4 MB: several pieces
    pos = len(data) - 100
    edit = data[:pos] + data[pos:].replace(b"xxxxx", b"yyyyy", 1)
    changed: list[bool] = []

    def rewrite(info: Any) -> None:
        if not changed and info.done:
            changed.append(True)
            rewrite_keeping_size_and_mtime(p, edit)   # valid JSON, same size, same mtime

    with Document.open(p) as doc:
        with pytest.raises(FileChangedError):
            doc.build_index(progress=Progress(callback=rewrite, min_interval=0))
        assert changed and not doc.index_complete and doc.record_count == 0
    assert jri_files() == []
    assert not [n for n in os.listdir(appdirs.index_dir()) if n.endswith((".tmp", ".lock"))]


@windows_only
def test_check_changed_waits_for_a_briefly_locked_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    p = write(tmp_path, b'[{"a": 1}]')
    with Document.open(p) as doc:
        handle = k32.CreateFileW(str(p), 0x80000000, 0, None, 3, 0x80, None)   # exclusive, like a backup tool
        assert handle not in (None, wintypes.HANDLE(-1).value)
        threading.Timer(0.3, k32.CloseHandle, args=(handle,)).start()
        t0 = time.perf_counter()
        assert not doc.check_changed()          # retried until the other program let go
        assert time.perf_counter() - t0 >= 0.2
        monkeypatch.setattr(reader, "CHANGE_RETRY_SECONDS", 0.1)
        handle = k32.CreateFileW(str(p), 0x80000000, 0, None, 3, 0x80, None)
        try:
            assert doc.check_changed()          # locked for good: it cannot be trusted
        finally:
            k32.CloseHandle(handle)


# ==================================================================================
# detection: first record bigger than the head (blackbox F2, CR-03, requirements F12)
# ==================================================================================
def _big_record(n: int = 3000) -> dict[str, Any]:
    return {"id": 0, "blob": "b" * n, "items": [{"k": i} for i in range(50)]}


def test_jsonl_whose_first_record_is_bigger_than_the_head(tmp_path: Path, backend: str) -> None:
    recs = [_big_record()] + [{"id": i} for i in range(1, 6)]
    p = write(tmp_path, b"\n".join(codec.dumps(r) for r in recs) + b"\n", "mongo_export.json")
    small = Settings(head_bytes=1000)
    info = detect_format(p, settings=small)
    assert info.kind == "jsonl"     # was "wrapped" with record path "items"
    with Document.open(p, settings=small) as doc:
        doc.build_index()
        assert doc.error is None and doc.record_count == 6 and doc.format_hint is None
        assert doc.record(0) == recs[0] and doc.record(5) == {"id": 5}


def test_jsonl_whose_records_are_bigger_than_half_the_head(tmp_path: Path, backend: str) -> None:
    recs = [{"id": i, "blob": "c" * 700} for i in range(3)]
    p = write(tmp_path, b"\r\n".join(codec.dumps(r) for r in recs), "lines.json")
    small = Settings(head_bytes=1000)
    assert detect_format(p, settings=small).kind == "jsonl"   # was "single"
    with Document.open(p, settings=small) as doc:
        doc.build_index()
        assert doc.error is None and doc.record_count == 3


def test_concatenated_values_with_a_big_multi_line_first_value(tmp_path: Path, backend: str) -> None:
    data = json.dumps(_big_record(), indent=1).encode() + b'\n{"id": 1}\n{"id": 2}\n'
    p = write(tmp_path, data, "stream.json")
    small = Settings(head_bytes=1000)
    assert detect_format(p, settings=small).kind == "stream"
    with Document.open(p, settings=small) as doc:
        doc.build_index()
        assert doc.error is None and doc.record_count == 3


def test_big_single_documents_are_not_scanned_beyond_the_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"id": i, "name": "n" * 30} for i in range(20_000)]
    files = {
        "wrapped.json": (codec.dumps({"meta": {"v": 1}, "data": rows}), "wrapped"),
        "array.json": (json.dumps(rows, indent=2).encode() + b"\n", "array"),
        "single.json": (codec.dumps({"a": "s" * 500_000, "b": 1}), "single"),
    }
    read = [0]
    real = reader.read_range

    def counting(f: Any, start: int, end: int) -> bytes:
        read[0] += end - start
        return real(f, start, end)

    monkeypatch.setattr(reader, "read_range", counting)
    small = Settings(head_bytes=4096)
    for name, (data, kind) in files.items():
        p = write(tmp_path, data, name)
        read[0] = 0
        assert detect_format(p, settings=small).kind == kind
        assert read[0] <= small.head_bytes + reader.TAIL_PROBE_BYTES, (name, read[0])


def test_format_hint_when_the_first_value_is_too_big_to_detect(tmp_path: Path, backend: str,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "EXTEND_SCAN_BYTES", 1500)
    big = {"id": 0, "blob": "z" * 3000}
    small = Settings(head_bytes=1000)
    p = write(tmp_path, codec.dumps(big) + b'\n{"id": 1}\n{"id": 2}\n', "hint.json")
    with Document.open(p, settings=small) as doc:
        assert doc.info.kind == "single"
        doc.build_index()
        assert doc.error is not None and doc.error.code == "trailing_data" and doc.record_count == 1
        assert doc.format_hint == "jsonl"
    with Document.open(p, settings=small) as doc:   # the cached index keeps the hint
        assert doc.index_from_cache and doc.format_hint == "jsonl"
    with Document.open(p, settings=small, format="single") as doc:   # a forced format: no hint
        doc.build_index()
        assert doc.error is not None and doc.format_hint is None
    q = write(tmp_path, json.dumps(big, indent=2).encode() + b'\n\n{"id": 1}\n', "hint_stream.json")
    with Document.open(q, settings=small) as doc:
        doc.build_index()
        assert doc.format_hint == "stream"
    r = write(tmp_path, codec.dumps(big) + b' {"id": 1}', "same_line.json")
    with Document.open(r, settings=small) as doc:
        doc.build_index()
        assert doc.format_hint == "stream"
    g = write(tmp_path, codec.dumps(big) + b"\n] garbage\n", "garbage.json")
    with Document.open(g, settings=small) as doc:
        doc.build_index()
        assert doc.error is not None and doc.format_hint is None


# ==================================================================================
# record paths (blackbox F11, codereview CR-01) and candidates (codereview CR-11)
# ==================================================================================
@pytest.mark.parametrize("rp", ["/data/0", "data.0", "data[0]", "$.data[0]", ("data", "0"), ("data", 0)])
def test_pointer_and_dotted_indices_select_array_elements(tmp_path: Path, backend: str, rp: Any) -> None:
    p = write(tmp_path, b'{"data": [[{"a": 1}, {"a": 2}], [{"a": 3}]]}')
    with Document.open(p, record_path=rp) as doc:
        assert doc.info.record_path == ("data", 0)
        doc.build_index()
        assert doc.error is None
        assert [doc.record(i) for i in range(doc.record_count)] == [{"a": 1}, {"a": 2}]


def test_numeric_keys_of_objects_stay_keys(tmp_path: Path, backend: str) -> None:
    p = write(tmp_path, b'{"data": {"0": [{"a": 1}], "1": [{"a": 2}]}}')
    with Document.open(p, record_path="data.0") as doc:
        assert doc.info.record_path == ("data", "0")
        doc.build_index()
        assert doc.error is None and doc.record_count == 1 and doc.record(0) == {"a": 1}


def test_array_root_with_a_pointer_record_path(tmp_path: Path, backend: str) -> None:
    p = write(tmp_path, b'[{"rows": [{"a": 1}, {"a": 2}]}, {"rows": []}]')
    with Document.open(p, record_path="/0/rows") as doc:
        assert doc.info.record_path == (0, "rows")
        doc.build_index()
        assert [doc.record(i) for i in range(doc.record_count)] == [{"a": 1}, {"a": 2}]


def test_container_beyond_the_head(tmp_path: Path, backend: str) -> None:
    small = Settings(head_bytes=500)
    pad = codec.dumps({"pad": "p" * 3000})
    arr = write(tmp_path, b'{"meta": ' + pad + b', "data": [[{"a": 1}], [{"a": 2}]]}', "arr.json")
    with Document.open(arr, record_path="/data/1", settings=small) as doc:
        assert doc.info.record_path == ("data", 1)   # not seen in the head: an index
        doc.build_index()
        assert doc.error is None and doc.record(0) == {"a": 2}
    obj = write(tmp_path, b'{"meta": ' + pad + b', "data": {"0": [{"a": 1}, {"a": 2}]}}', "obj.json")
    with Document.open(obj, record_path="/data/0", settings=small) as doc:
        assert doc.info.record_path == ("data", 0)
        doc.build_index()       # data[0] is not found: the member key "0" is tried
        assert doc.error is None and doc.info.record_path == ("data", "0") and doc.record_count == 2
    with Document.open(obj, record_path="data.0", settings=small) as doc:   # cached
        assert doc.index_complete and doc.info.record_path == ("data", "0") and doc.record_count == 2
        alt_key = doc._main_key()
    for name in os.listdir(appdirs.index_dir()):
        os.remove(appdirs.index_dir() / name)
    with Document.open(obj, record_path="/data/0", settings=small) as doc:
        assert not doc.index_complete and doc.info.record_path == ("data", 0)
        # meanwhile "another process" indexes data[0] (as an index): not found
        with Document.open(obj, record_path=("data", 0), settings=small) as other:
            other.build_index()
            assert other.error is not None and other.error.code == "target_not_found"
        doc.build_index()   # finds that index, switches to the member key, builds under its own key
        assert doc.error is None and doc.record_count == 2 and doc._main_key() == alt_key
    with Document.open(obj, record_path=("data", 0), settings=small) as doc:   # data[0]'s index is intact
        assert doc.index_complete and doc.error is not None and doc.error.code == "target_not_found"
    with Document.open(obj, record_path="/data/7", settings=small) as doc:  # neither exists
        doc.build_index()
        assert doc.error is not None and doc.error.code == "target_not_found"


def test_candidates_whose_items_are_arrays(tmp_path: Path) -> None:
    p = write(tmp_path, b'{"m": [[1, 2], [3]], "o": [{"a": 1}], "v": [1, "x"], "x": [[1], {"a": 1}], '
                        b'"s": [[1], 2], "e": []}')
    cands = {c.text: c for c in detect_format(p).candidates}
    kinds = {name: c.items_kind for name, c in cands.items()}
    assert kinds == {"m": "arrays", "o": "objects", "v": "values", "x": "mixed", "s": "mixed", "e": "empty"}
    m = cands["m"]
    assert m.arrays_in_head == 2 and m.objects_in_head == 0
    assert RecordPathCandidate.from_dict(m.to_dict()) == m
    assert RecordPathCandidate.from_dict({k: v for k, v in m.to_dict().items() if k != "arrays_in_head"}).items_kind \
        == "values"   # an old cached candidate without the count


# ==================================================================================
# error offsets of converted inputs (blackbox F15, CR-13, requirements F15)
# ==================================================================================
@pytest.mark.parametrize("encoding, bom", [("utf-16-le", True), ("utf-16-le", False),
                                           ("utf-16-be", True), ("utf-16-be", False)])
def test_utf16_error_offset_is_a_byte_offset_of_the_users_file(tmp_path: Path, backend: str, lang_en,
                                                               encoding: str, bom: bool) -> None:
    text = '[{"name": "Тошкент \U0001F600"},\n {"a": 1 x}]'
    boms = {"utf-16-le": codecs.BOM_UTF16_LE, "utf-16-be": codecs.BOM_UTF16_BE}
    raw = (boms[encoding] if bom else b"") + text.encode(encoding)
    p = write(tmp_path, raw, "u16.json")
    expected = (2 if bom else 0) + len(text[:text.index(" x}") + 1].encode(encoding))
    assert raw[expected:expected + 2].decode(encoding) == "x"
    with Document.open(p) as doc:
        doc.build_index()
        err = doc.error
        assert err is not None and (err.line, err.column) == (2, 10)
        assert err.offset == expected and f"byte {expected}" in err.message
        with open(doc.data_path, "rb") as f:   # the offset in the UTF-8 copy stays available
            f.seek(doc.error_data_offset)
            assert f.read(1) == b"x"
    with Document.open(p) as doc:
        assert doc.index_from_cache and doc.error.offset == expected and doc.error_data_offset is not None


def test_gzip_error_offset_is_one_of_the_decompressed_data(tmp_path: Path, backend: str, lang_en) -> None:
    text = b'[{"a": 1},\n {"b": 2 x}]'
    for name, content, offset in (("plain.json.gz", text, text.index(b"x")),
                                  ("bom.json.gz", codecs.BOM_UTF8 + text, 3 + text.index(b"x")),
                                  ("u16.json.gz", codecs.BOM_UTF16_LE + text.decode().encode("utf-16-le"),
                                   2 + 2 * text.index(b"x"))):
        p = write(tmp_path, gzip.compress(content), name)
        with Document.open(p) as doc:
            doc.build_index()
            err = doc.error
            assert err is not None and err.offset == offset, name
            assert err.key == "reader.error.json_syntax_decompressed"
            assert f"byte {offset} of the decompressed data" in err.message and err.hint
        with Document.open(p) as doc:   # cached: same wording
            assert doc.index_from_cache and "decompressed data" in doc.error.message


def test_gzip_error_wording_in_uzbek(tmp_path: Path, lang_uz) -> None:
    p = write(tmp_path, gzip.compress(b"[1, x]"), "e.json.gz")
    with Document.open(p) as doc:
        doc.build_index()
        assert "arxivdan ochilgan ma'lumotning 4-bayti" in doc.error.message


# ==================================================================================
# block reads: density and progress (blackbox F5, F10)
# ==================================================================================
@pytest.mark.parametrize("mapped", [False, True], ids=["read", "map"])
def test_low_cardinality_sort_order_reads_about_the_selected_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                   mapped: bool) -> None:
    monkeypatch.setattr(reader, "MAP_SCATTERED", mapped)
    recs = [{"id": i, "city": f"c{i % 16}", "pad": "p" * 280} for i in range(6000)]
    p = write(tmp_path, codec.dumps(recs))
    with Document.open(p) as doc:
        doc.build_index()
        order = sorted(range(6000), key=lambda i: i % 16)   # a stable sort: 16 sparse ascending runs
        selected = sum(doc.record_span(i)[1] - doc.record_span(i)[0] for i in order)
        read = [0]
        real = doc._read

        def counting(start: int, end: int, exact: bool = False) -> bytes:
            read[0] += max(0, end - start) if exact else max(reader.READ_AHEAD, end - start)
            return real(start, end, exact)

        monkeypatch.setattr(doc, "_read", counting)
        seen: list[int] = []
        progress = Progress(total=selected, callback=lambda info: seen.append(info.done), min_interval=0)
        got = []
        for blk in doc.iter_blocks(order, progress=progress):
            for k, i in enumerate(blk.indices):
                got.append(int(i))
                assert codec.loads(blk.record_bytes(k)) == recs[i]
        assert got == order
        # before: each of the 16 runs was read as one range from its first to its last record
        # (~16x); now only the first run is (a single pass has nothing to re-read), the later
        # ones read their records: about the file once plus the selection
        assert read[0] < 2.5 * selected, (read[0], selected)
        assert max(seen) <= selected and progress.done == selected
        # dense selections are still read in contiguous blocks
        blocks = list(doc.iter_blocks(list(range(6000))))
        assert len(blocks) == 1 and blocks[0].contiguous
        # a single ascending pass reads small gaps (cheaper than fetching records one by one)
        for step in (2, 3, 16):
            blocks = list(doc.iter_blocks(list(range(0, 6000, step))))
            assert all(b.contiguous for b in blocks) and len(blocks) == 1, step
        # ... but skips big ones (~13 KB between the records here)
        blocks = list(doc.iter_blocks(list(range(0, 6000, 40))))
        assert not any(b.contiguous for b in blocks)


def test_iter_blocks_progress_never_exceeds_the_selection(tmp_path: Path) -> None:
    data = json.dumps([{"id": i, "v": "w" * (i % 50)} for i in range(2000)], indent=4).encode()
    p = write(tmp_path, data)
    with Document.open(p) as doc:
        doc.build_index()
        n = doc.record_count
        sources = [list(range(n)), list(range(n - 1, -1, -1)), range(10, 1500), list(range(0, n, 9)),
                   [5, 5, 5, 1999, 0]]
        for source in sources:
            selected = sum(doc.record_span(i)[1] - doc.record_span(i)[0] for i in source)
            seen: list[int] = []
            progress = Progress(total=selected, callback=lambda info: seen.append(info.done), min_interval=0)
            blocks = list(doc.iter_blocks(source, block_bytes=20_000, progress=progress))
            assert sum(b.selected_bytes for b in blocks) == selected
            assert max(seen) <= selected and progress.done == selected, source
        seen = []
        progress = Progress(callback=lambda info: seen.append(info.done), min_interval=0)
        list(doc.iter_blocks(None, progress=progress))
        assert progress.total == doc.data_size and max(seen) <= doc.data_size


# ==================================================================================
# unusable folders, long paths, logging (blackbox F6, robustness F7, robustness F12)
# ==================================================================================
@pytest.fixture
def private_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    temp = tmp_path / "system_temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    appdirs._reset_for_tests()
    yield temp
    appdirs._reset_for_tests()


def test_unusable_app_home_falls_back_to_a_temp_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       private_temp: Path) -> None:
    blocker = tmp_path / "home_is_a_file"
    blocker.write_bytes(b"x")
    monkeypatch.setenv("JSONRESHAPE_HOME", str(blocker))
    p = write(tmp_path, b'[{"a": 1}, {"a": 2}]')
    with Document.open(p) as doc:
        doc.build_index()
        assert doc.record_count == 2
        assert doc.index_path.parent == private_temp / "jsonreshape" / "index"
        assert doc.info.warnings.count("fileio.warning.app_dir_fallback") == 1
        idx = doc.info.warnings.index("fileio.warning.app_dir_fallback")
        params = doc.info.warning_params[idx]
        assert params["path"] == str(blocker) and params["fallback"] == str(private_temp / "jsonreshape")
        assert params["detail"] == t("fileio.reason.not_a_folder")   # localized, not the OS text
    assert appdirs.app_home() == private_temp / "jsonreshape"
    with Document.open(p) as doc:   # reported once per process
        assert "fileio.warning.app_dir_fallback" not in doc.info.warnings
    assert Settings.load().language == "uz"   # settings fall back to the defaults quietly


def test_unusable_cache_dir_falls_back_to_the_default_index_folder(tmp_path: Path, private_temp: Path,
                                                                   lang_en) -> None:
    blocker = tmp_path / "cache_blocker"
    blocker.write_bytes(b"x")
    s = Settings(cache_dir=str(blocker / "cache"))   # below a file: cannot be created
    p = write(tmp_path, b'[{"a": 1}, {"a": 2}]')
    for use_cache in (True, False):
        with Document.open(p, settings=s, use_cache=use_cache) as doc:
            doc.build_index()
            assert doc.record_count == 2 and doc.index_path.parent == appdirs.index_dir()
            if use_cache:
                texts = doc.info.warning_texts()
                assert any("The cache folder cannot be used" in w and str(blocker / "cache") in w for w in texts)
    gz = write(tmp_path, gzip.compress(b"[1, 2, 3]"), "d.json.gz")
    with Document.open(gz, settings=s) as doc:   # prepared copies fall back as well
        doc.build_index()
        assert doc.record_count == 3 and doc.data_path.parent == appdirs.tmp_dir() / "prepared"


def test_no_usable_folder_at_all_is_a_localized_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      lang_en) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")
    monkeypatch.setenv("JSONRESHAPE_HOME", str(blocker / "home"))
    monkeypatch.setattr(tempfile, "tempdir", str(blocker / "temp"))
    appdirs._reset_for_tests()
    try:
        with pytest.raises(JsonReshapeError) as info:
            appdirs.app_home()
        assert info.value.key == "fileio.error.no_work_dir" and info.value.hint
        assert Settings.load() == Settings()   # never raises
    finally:
        appdirs._reset_for_tests()


def test_long_folders_without_long_path_support(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                private_temp: Path) -> None:
    """Emulates an interpreter without ``LongPathsEnabled``: creating an unprefixed folder
    of MAX_PATH or more fails, as it does there (robustness F7)."""
    real_mkdir = os.mkdir

    def mkdir(path: Any, mode: int = 0o777, *, dir_fd: Any = None) -> None:
        s = os.fspath(path)
        if WINDOWS and len(os.path.abspath(s)) >= 260 and not s.startswith("\\\\?\\"):
            raise FileNotFoundError(206, "The filename or extension is too long", s)
        real_mkdir(path, mode)

    monkeypatch.setattr(os, "mkdir", mkdir)
    deep = tmp_path.joinpath(*(["long_folder_name_" + "x" * 30] * 7))
    assert len(str(deep)) > 300
    monkeypatch.setenv("JSONRESHAPE_HOME", str(deep / "home"))
    appdirs._reset_for_tests()
    try:
        s = Settings(cache_dir=str(deep / "cache"))
        p = write(tmp_path, b'[{"a": 1}]')
        with Document.open(p, settings=s) as doc:
            doc.build_index()
            assert doc.index_path.parent == deep / "cache"
            assert "fileio.warning.cache_dir_fallback" not in doc.info.warnings
        assert appdirs.logs_dir() == deep / "home" / "logs"
        gz = write(tmp_path, gzip.compress(b"[1]"), "g.json.gz")
        with Document.open(gz, settings=s) as doc:
            assert doc.data_path.parent == deep / "cache" / "tmp" / "prepared"
    finally:
        appdirs._reset_for_tests()


@pytest.fixture
def clean_logging():
    root = logging.getLogger()
    level = root.level
    handlers = list(root.handlers)
    yield
    jlog.shutdown_logging()
    root.setLevel(level)
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)


def test_log_file_that_cannot_be_opened_switches_file_logging_off_quietly(
        clean_logging, capfd: pytest.CaptureFixture[str]) -> None:
    path = jlog.log_path()
    path.mkdir(parents=True)          # a folder where the log file should be
    jlog.setup_logging("INFO")        # its own first record already fails
    log = logging.getLogger("jsonreshape.test_fix")
    for i in range(5):
        log.warning("warning %d", i)
        log.error("with a traceback", exc_info=ValueError("boom"))
    err = capfd.readouterr().err
    assert "Logging error" not in err and "Traceback" not in err and "warning 3" not in err
    handler = jlog._file_handler
    assert handler is not None and handler.disabled_reason


_NO_FOLDER = r"""
import logging, sys, tempfile
tempfile.tempdir = sys.argv[1]         # an unusable temporary folder too
from jsonreshape import log as jlog
path = jlog.setup_logging("INFO")      # no usable folder at all: must not raise
logging.getLogger("jsonreshape.x").warning("not on stderr", exc_info=ValueError("boom"))
logging.getLogger("other.library").error("nor this")
print("setup returned", path)
"""


def test_failed_logging_setup_never_prints_tracebacks(tmp_path: Path) -> None:
    # outside pytest (whose own handlers hide logging.lastResort): no data folder, no temp folder
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"x")
    env = dict(os.environ, JSONRESHAPE_HOME=str(blocker / "home"), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-c", _NO_FOLDER, str(blocker / "temp")], env=env, cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stderr
    assert "setup returned" in r.stdout
    assert "not on stderr" not in r.stderr and "nor this" not in r.stderr and "Traceback" not in r.stderr


def test_cli_with_an_unusable_data_folder(tmp_path: Path) -> None:
    blocker = tmp_path / "home_file"
    blocker.write_bytes(b"x")
    temp = tmp_path / "temp"
    temp.mkdir()
    p = write(tmp_path, b'[{"a": 1}, {"a": 2}]')
    env = dict(os.environ, JSONRESHAPE_HOME=str(blocker), TEMP=str(temp), TMP=str(temp), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-m", "jsonreshape", "--lang", "en", "inspect", str(p)], env=env,
                       capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=300)
    assert r.returncode == 0, r.stderr
    assert "Traceback" not in r.stderr and "Logging error" not in r.stderr
    assert "The app data folder cannot be used" in r.stdout + r.stderr
    assert "a file of that name is in the way" in r.stdout + r.stderr
    assert (temp / "jsonreshape" / "logs" / "jsonreshape.log").exists()


# ==================================================================================
# several processes, one index (requirements F7, robustness F3, robustness F14)
# ==================================================================================
def _spans(n: int) -> bytes:
    from array import array

    return array("Q", [v for i in range(n) for v in (10 * i, 10 * i + 7)]).tobytes()


def _build(path: Path, n: int, meta: dict[str, Any] | None = None) -> IndexReader:
    w = IndexWriter(path)
    w.append(_spans(n))
    return w.finalize(meta or {"kind": "test"})


def test_build_lock_is_exclusive_and_leaves_no_file(tmp_path: Path) -> None:
    path = lock_path("ab" * 32, tmp_path)
    a, b = BuildLock(path), BuildLock(path)
    assert a.acquire() and a.acquire() and not b.acquire()
    a.release()
    assert not path.exists()
    assert b.acquire()
    b.release()
    assert os.listdir(tmp_path) == []


def test_finalize_retries_and_never_deletes_the_installed_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    final = tmp_path / "k.jri"
    _build(final, 3).close()
    real = IndexReader.open.__func__
    calls = [0]

    def flaky(cls: Any, path: Any, *, touch: bool = True) -> IndexReader | None:
        calls[0] += 1
        if calls[0] <= 2:   # e.g. another process is replacing the same name right now
            return None
        return real(cls, path, touch=touch)

    monkeypatch.setattr(IndexReader, "open", classmethod(flaky))
    r = _build(final, 5)
    assert r.count == 5 and calls[0] >= 3
    r.close()
    monkeypatch.setattr(IndexReader, "open", classmethod(lambda cls, path, *, touch=True: None))
    monkeypatch.setattr(ix, "OPEN_RETRY_SECONDS", 0.05)
    with pytest.raises(IndexBuildError):
        _build(final, 7)
    assert final.exists()   # never deleted: it may be another process's index
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]


def test_installing_a_canonical_index_removes_old_duplicates(tmp_path: Path) -> None:
    key = "ef" * 32
    final = index_path(key, tmp_path)
    alt = tmp_path / f"{key}~0123abcd.jri"
    _build(alt, 2).close()
    _build(final, 4).close()
    assert os.listdir(tmp_path) == [final.name]


def test_eviction_removes_duplicates_first(tmp_path: Path) -> None:
    key = "cd" * 32
    final = index_path(key, tmp_path)
    _build(final, 400).close()
    alt = tmp_path / f"{key}~00ff00ff.jri"
    _build(alt, 400).close()   # newer than the canonical file, but never read again
    other = tmp_path / ("12" * 32 + ".jri")
    _build(other, 400).close()
    old = time.time() - 3600
    os.utime(final, (old, old))
    os.utime(other, (old, old))
    size = final.stat().st_size
    evict(2 * size + 100, directory=tmp_path)
    assert not alt.exists() and final.exists() and other.exists()


def test_rebuild_while_the_index_is_mapped_keeps_no_duplicate(tmp_path: Path) -> None:
    p, _ = records_file(tmp_path, n=2000)
    with Document.open(p) as d1:
        d1.build_index()                                  # mapped by d1
        with Document.open(p, use_cache=False) as d2:     # --no-cache while d1 is open
            d2.build_index()
            assert d2.record_count == d1.record_count
            assert d2.index_path == d1.index_path         # Windows: reused, not duplicated
        assert jri_files() == [d1.index_path.name]


def test_a_waiting_document_uses_the_index_built_meanwhile(tmp_path: Path) -> None:
    p, _ = records_file(tmp_path, n=2000)
    other = Document.open(p)
    key, directory = other._main_key(), other._index_dir()
    held = BuildLock(lock_path(key, directory))
    assert held.acquire()   # "another process" is building this index
    messages: list[str] = []
    with Document.open(p) as doc:
        progress = Progress(callback=lambda info: messages.append(info.message), min_interval=0)
        task = threading.Thread(target=doc.build_index, kwargs={"progress": progress})
        task.start()
        deadline = time.monotonic() + 10
        while not any("another process" in m or "Boshqa jarayon" in m for m in messages):
            assert time.monotonic() < deadline, messages
            time.sleep(0.01)
        assert not doc.index_complete
        other._build_scan(None, None, index_path(key, directory))   # the other one finishes
        other.close()
        held.release()
        task.join(30)
        assert doc.index_complete and doc.index_from_cache and doc.record_count == 2000
    assert jri_files() == [index_path(key, directory).name]


_WORKER = r"""
import json, sys
from jsonreshape.reader import Document
with Document.open(sys.argv[1]) as doc:
    doc.build_index()
    print(json.dumps({"count": doc.record_count, "cached": doc.index_from_cache, "error": doc.error is not None}))
"""


@pytest.mark.slow
def test_several_processes_index_the_same_file(tmp_path: Path) -> None:
    rec = codec.dumps({"id": 1, "name": "process race", "tags": [1, 2, 3], "v": 1.5})
    p = tmp_path / "shared.json"
    with open(p, "wb") as f:
        f.write(b"[" + b",".join([rec] * 150_000) + b"]")   # ~10 MB
    home = tmp_path / "home"
    env = dict(os.environ, JSONRESHAPE_HOME=str(home), PYTHONIOENCODING="utf-8")
    for _round in range(2):
        for name in os.listdir(home / "index") if (home / "index").exists() else ():
            os.remove(home / "index" / name)
        procs = [subprocess.Popen([sys.executable, "-c", _WORKER, str(p)], env=env, cwd=ROOT,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(6)]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=600)
            assert proc.returncode == 0, err.decode("utf-8", "replace")
            results.append(json.loads(out.decode().strip().splitlines()[-1]))
        assert all(r["count"] == 150_000 and not r["error"] for r in results)
        names = os.listdir(home / "index")
        assert [n for n in names if n.endswith(".jri")] and len([n for n in names if n.endswith(".jri")]) == 1, names
        assert not [n for n in names if n.endswith((".tmp", ".lock"))], names


# ==================================================================================
# prepared copies under the cache folder, one budget (blackbox F9, requirements F8, CR-08)
# ==================================================================================
def test_prepared_copies_live_under_the_cache_folder(tmp_path: Path) -> None:
    cache = tmp_path / "my_cache"
    s = Settings(cache_dir=str(cache))
    gz = write(tmp_path, gzip.compress(codec.dumps([{"a": i} for i in range(100)])), "d.json.gz")
    with Document.open(gz, settings=s) as doc:
        doc.build_index()
        assert doc.data_path.parent == cache / "tmp" / "prepared"
        assert doc.index_path.parent == cache
    home_prepared = appdirs.tmp_dir() / "prepared"
    assert not home_prepared.exists() or os.listdir(home_prepared) == []


def test_index_files_and_prepared_copies_share_one_budget(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    big = Settings(cache_dir=str(cache), cache_limit_bytes=10 ** 9)
    gz = write(tmp_path, gzip.compress(codec.dumps([{"a": "q" * 100, "i": i} for i in range(3000)])), "a.json.gz")
    with Document.open(gz, settings=big) as doc:
        doc.build_index()
        prepared, index_file = doc.data_path, doc.index_path
    old = time.time() - 7200   # used two hours ago: not protected any more
    for f in (prepared.with_suffix(".meta"), index_file):
        os.utime(f, (old, old))
    p, _ = records_file(tmp_path, n=200)
    tiny = Settings(cache_dir=str(cache), cache_limit_bytes=1)
    with Document.open(p, settings=tiny) as doc:
        doc.build_index()       # its own index stays (in use); the old ones go
        assert doc.index_path.exists()
    assert not prepared.exists() and not prepared.with_suffix(".meta").exists() and not index_file.exists()
    # and the other way round: preparing a new copy evicts an old index file
    with Document.open(p, settings=big, use_cache=False) as doc:
        doc.build_index()
        index_file = doc.index_path
    os.utime(index_file, (old, old))
    gz2 = write(tmp_path, gzip.compress(b"[1, 2]"), "b.json.gz")
    pf = prepare_data_file(gz2, settings=tiny)
    assert pf.data_path.exists() and not index_file.exists()


# ==================================================================================
# fileio messages (robustness F13), surrogate file names (robustness F15)
# ==================================================================================
@windows_only
def test_read_only_output_says_so(tmp_path: Path, lang_en) -> None:
    target = tmp_path / "out.jsonl"
    target.write_bytes(b"old")
    os.chmod(target, stat.S_IREAD)
    try:
        with pytest.raises(UserInputError) as info:
            with atomic_write(target) as f:
                f.write(b"new")
        assert info.value.key == "fileio.error.read_only" and "read-only" in str(info.value)
        assert "Excel" not in str(info.value) and info.value.hint
        assert target.read_bytes() == b"old"
        assert fileio.os_error(PermissionError(13, "denied"), target).key == "fileio.error.read_only"
    finally:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    assert os.listdir(tmp_path) == ["out.jsonl"]


def test_write_errors_name_the_output_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lang_en) -> None:
    target = tmp_path / "out.json"
    with pytest.raises(JsonReshapeError) as info:
        with atomic_write(target) as f:
            f.write(b"[1")
            raise OSError(errno.ENOSPC, "No space left on device")
    assert info.value.key == "fileio.error.disk_full" and info.value.params["path"] == str(target)
    assert info.value.hint
    with pytest.raises(JsonReshapeError) as info:
        with atomic_path(target):
            raise OSError(errno.ENOSPC, "No space left on device")
    assert info.value.key == "fileio.error.disk_full"
    with pytest.raises(OSError) as raw:      # an error of something else (reading an input) passes
        with atomic_write(target):
            raise OSError(errno.EIO, "reading the input failed")
    assert not isinstance(raw.value, JsonReshapeError)

    def failing_fsync(fd: int) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(fileio.os, "fsync", failing_fsync)
    with pytest.raises(JsonReshapeError) as info:
        with atomic_write(target) as f:
            f.write(b"[]")
    assert info.value.key == "fileio.error.write" and info.value.params["path"] == str(target)
    assert "I/O error" in str(info.value) and info.value.hint
    assert os.listdir(tmp_path) == []


@windows_only
def test_gzip_input_with_a_lone_surrogate_in_its_name(tmp_path: Path) -> None:
    p = tmp_path / ("data_" + chr(0xD83D) + "_x.json.gz")
    try:
        p.write_bytes(gzip.compress(b"[1, 2]"))
    except (OSError, UnicodeError):
        pytest.skip("the file system refuses the name")
    pf = prepare_data_file(p)
    meta = pf.data_path.with_suffix(".meta").read_bytes()
    assert json.loads(meta.decode("ascii"))["source"] == str(p)
    with Document.open(p) as doc:
        doc.build_index()
        assert doc.record_count == 2
