"""jsonreshape.reader.Document (docs/ARCHITECTURE.md §5.1, §5.3; AGENT_PROMPT §3).

* format detection heuristics (table), record-path candidates and the automatic choice,
  forced formats / record paths and their errors;
* live record counts and record access while the index is built in the background
  ("birinchi yozuvlar darhol ko'rinadi"), cancellation, closing during a build;
* change detection and reopening; the index cache (reuse, ``use_cache=False``, eviction);
* record access API, ``iter_blocks`` / ``iter_records`` / ``iter_record_bytes`` with every
  kind of source, huge records, wrapper parts, thread safety.
"""

from __future__ import annotations

import gzip
import os
import random
import threading
import time
from array import array
from pathlib import Path

import pytest

import jsongen
from jsonreshape import codec, reader
from jsonreshape.core import FLAG_ESCAPES, FLAG_NON_ASCII, FLAG_U_ESCAPES
from jsonreshape.errors import (
    Cancelled,
    FileFormatError,
    PathSyntaxError,
    TooLargeError,
    UserInputError,
)
from jsonreshape.i18n import set_language
from jsonreshape.index import find_index
from jsonreshape.reader import Block, Document, FormatInfo, RecordPathCandidate, detect_format
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress


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


def index_dir_files() -> list[str]:
    from jsonreshape.appdirs import index_dir

    return sorted(os.listdir(index_dir()))


# ----------------------------------------------------------------------------------
# detection heuristics
# ----------------------------------------------------------------------------------
DETECT = [
    ("array", b"[1, 2, 3]", "array", None),
    ("array-leading-ws", b"  \r\n [ ]", "array", None),
    ("array-then-garbage", b"[1] x", "array", None),
    ("object-single", b'{"a": 1, "b": "x"}', "single", None),
    ("empty-object", b"{}", "single", None),
    ("empty-object-ws", b"{ \n }", "single", None),
    ("scalar-string", b'"abc"', "single", None),
    ("scalar-number", b" 123 ", "single", None),
    ("scalar-literal", b"true", "single", None),
    ("wrapped-data", b'{"data": [{"x": 1}, {"x": 2}]}', "wrapped", ("data",)),
    ("wrapped-meta-tail", b'{"meta": {"v": 1}, "items": [{"x": 1}, {"x": 2}, {"x": 3}], "count": 3}', "wrapped",
     ("items",)),
    # the record array holds more than half of all values (10 of 17): it beats the keyed test
    ("wrapped-dominant-nested",
     b'{"meta": {"v": 1}, "a": {"b": [{"x": 1}, {"x": 2}, {"x": 3}, {"x": 4}, {"x": 5}]}, "tail": {"n": 3}}',
     "wrapped", ("a", "b")),
    # ... but 6 of 13 values do not dominate: all three members are objects -> keyed
    ("keyed-not-dominated", b'{"meta": {"v": 1}, "a": {"b": [{"x": 1}, {"x": 2}, {"x": 3}]}, "tail": {"n": 3}}',
     "keyed", None),
    ("wrapped-dominant-depth3", b'{"r": {"s": {"t": [{"x": 1}, {"x": 2}, {"x": 3}]}}}', "wrapped", ("r", "s", "t")),
    # arrays of scalars in a small config-like object are not records -> single
    ("scalars-not-records", b'{"a": [1, 2, 3], "b": [4, 5]}', "single", None),
    ("wrapped-objects-rule-c", b'{"a": [{"x": 1}, {"x": 2}, {"x": 3}], "b": [4, 5], "c": 1, "d": 2, "e": 3}', "wrapped", ("a",)),
    ("wrapped-tie-shallowest", b'{"x": {"deep": [{"a": 1}, {"a": 2}]}, "top": [{"b": 3}, {"b": 4}], "n": 1}',
     "wrapped", ("top",)),
    ("keyed", b'{"u1": {"n": 1}, "u2": {"n": 2}, "u3": {"n": 3}}', "keyed", None),
    ("keyed-80-percent", b'{"a": {}, "b": {}, "c": {}, "d": {}, "v": 1}', "keyed", None),
    ("not-keyed-75-percent", b'{"a": {}, "b": {}, "c": {}, "v": 1}', "single", None),
    ("keyed-inner-arrays", b'{"u1": {"tags": [1, 2]}, "u2": {"tags": [3]}, "u3": {"tags": []}}', "keyed", None),
    ("one-item-array", b'{"x": 1, "items": [{"a": 1}]}', "single", None),
    ("brace-objects", b'{ {"a": 1}, {"b": 2} }', "brace_list", None),
    ("brace-strings", b'{"a", "b"}', "brace_list", None),
    ("brace-one-string", b'{"a"}', "brace_list", None),
    ("brace-numbers", b"{1, -2}", "brace_list", None),
    ("brace-arrays", b"{[1], [2]}", "brace_list", None),
    ("jsonl", b'{"a": 1}\n{"a": 2}\n', "jsonl", None),
    ("jsonl-scalars", b"1\n2\n3", "jsonl", None),
    ("jsonl-crlf", b'{"a": 1}\r\n{"a": 2}\r\n', "jsonl", None),
    ("jsonl-broken-first-line", b'{"a": 1\n{"a": 2}\n{"a": 3}\n', "jsonl", None),
    ("stream-pretty", b'{\n "a": 1\n}\n{\n "a": 2\n}\n', "stream", None),
    ("stream-same-line", b'{"a": 1} {"a": 2}', "stream", None),
    ("stream-numbers", b"1 2 3", "stream", None),
    ("stream-arrays", b"[1]\n[2] [3]", "jsonl", None),
]


@pytest.mark.parametrize("case", DETECT, ids=[c[0] for c in DETECT])
def test_detection_table(tmp_path: Path, backend: str, case) -> None:
    _, data, kind, record_path = case
    p = write(tmp_path, data)
    info = detect_format(p)
    assert (info.kind, info.record_path) == (kind, record_path)
    assert info.auto and info.data_start == 0 and info.head_complete
    assert info.root_start == len(data) - len(data.lstrip(b" \t\r\n"))
    assert ("reader.warn_brace_list" in info.warnings) == (kind == "brace_list")
    assert ("reader.record_path_auto" in info.warnings) == (kind == "wrapped")
    with Document.open(p) as doc:
        assert doc.info.kind == kind and doc.info.record_path == record_path
        doc.build_index()
        assert doc.record_count >= 1 or kind == "array"


def test_candidates_are_listed_open_first_then_by_items(tmp_path: Path) -> None:
    body = b",".join(b'{"r": %d}' % i for i in range(200))
    data = (b'{"meta": {"tags": [1, 2, 3], "x": [{"a": 1}]}, "lookup": {"codes": [1, 2, 3, 4, 5]}, '
            b'"data": [' + body + b'], "tail": {"z": []}}')
    p = write(tmp_path, data)
    info = detect_format(p, settings=Settings(head_bytes=600))
    assert not info.head_complete
    assert info.kind == "wrapped" and info.record_path == ("data",)
    paths = [c.path for c in info.candidates]
    assert paths == [("data",), ("lookup", "codes"), ("meta", "tags"), ("meta", "x")]
    first = info.candidates[0]
    assert not first.closed_in_head and first.items_in_head > 5 and first.objects_in_head == first.items_in_head
    assert first.items_kind == "objects" and first.depth == 1 and first.text == "data"
    assert info.candidates[1] == RecordPathCandidate(("lookup", "codes"), True, 5, 0, 5)
    assert info.candidates[1].items_kind == "values" and info.candidates[3].items_kind == "objects"
    idx = info.warnings.index("reader.record_path_auto")
    assert info.warning_params[idx] == {"path": "data"}
    complete = detect_format(p)
    assert complete.head_complete and complete.kind == "wrapped" and complete.record_path == ("data",)
    assert complete.candidates[0].path == ("data",) and complete.candidates[0].items_in_head == 200
    assert all(c.closed_in_head for c in complete.candidates)


def test_big_file_heuristics_with_a_small_head(tmp_path: Path) -> None:
    small = Settings(head_bytes=400)
    recs = [{"id": i, "tags": [1, 2, 3], "name": "x" * 10} for i in range(100)]
    # keyed document whose head ends inside one record's inner array -> still keyed
    keyed = b"{" + b",".join(b'"u%d": ' % i + codec.dumps(r) for i, r in enumerate(recs)) + b"}"
    p = write(tmp_path, keyed, "keyed.json")
    for head in (380, 400, 417, 455):
        info = detect_format(p, settings=Settings(head_bytes=head))
        assert info.kind == "keyed", head
    # wrapped document: the open array wins (auto choice + warning)
    wrapped = b'{"meta": {"source": "x"}, "result": {"rows": [' + b",".join(codec.dumps(r) for r in recs) + b"]}}"
    p = write(tmp_path, wrapped, "wrapped.json")
    info = detect_format(p, settings=small)
    assert info.kind == "wrapped" and info.record_path == ("result", "rows") and not info.head_complete
    # the head ends inside a big "meta" object before any array: single
    big_meta = b'{"meta": {' + b",".join(b'"k%d": %d' % (i, i) for i in range(200)) + b'}, "data": [1, 2]}'
    p = write(tmp_path, big_meta, "meta.json")
    assert detect_format(p, settings=small).kind == "single"
    # big arrays / JSON Lines are recognized from the head alone
    p = write(tmp_path, b"[" + b",".join(codec.dumps(r) for r in recs) + b"]", "arr.json")
    assert detect_format(p, settings=small).kind == "array"
    p = write(tmp_path, b"\n".join(codec.dumps(r) for r in recs), "lines.json")
    assert detect_format(p, settings=small).kind == "jsonl"


def test_whitespace_prefix_longer_than_the_head(tmp_path: Path) -> None:
    p = write(tmp_path, b" " * 5000 + b'\n{"data": [{"a": 1}, {"a": 2}]}\n')
    info = detect_format(p, settings=Settings(head_bytes=1000))
    assert info.root_start == 5001 and info.kind == "wrapped"
    with Document.open(p, settings=Settings(head_bytes=1000)) as doc:
        doc.build_index()
        assert doc.record_count == 2 and doc.record(1) == {"a": 2}


def test_forced_options(tmp_path: Path) -> None:
    wrapped = write(tmp_path, b'{"meta": {"m": [1, 2]}, "rows": [{"a": 1}, {"a": 2}], "obj": {"k": {"v": 1}}}')
    info = detect_format(wrapped, record_path="rows")
    assert (info.kind, info.record_path, info.auto) == ("wrapped", ("rows",), False)
    assert "reader.record_path_auto" not in info.warnings and info.container_kind == "array"
    info = detect_format(wrapped, record_path="$.obj")
    assert (info.kind, info.record_path, info.container_kind) == ("wrapped", ("obj",), "object")
    info = detect_format(wrapped, record_path=["meta", "m"], format="wrapped")
    assert info.record_path == ("meta", "m")
    info = detect_format(wrapped, record_path="/meta/m")
    assert info.record_path == ("meta", "m")
    for rp in ("", "$", ()):
        assert detect_format(wrapped, record_path=rp).kind == "keyed"
    info = detect_format(wrapped, format="wrapped")
    assert info.record_path == ("rows",) and "reader.record_path_auto" in info.warnings and not info.auto
    assert detect_format(wrapped, format=" AUTO ").kind == "wrapped"
    assert detect_format(wrapped, format="single").kind == "single"
    assert detect_format(wrapped, format="keyed").kind == "keyed"
    arr = write(tmp_path, b"[1, 2]", "arr.json")
    assert detect_format(arr, record_path="$").kind == "array"
    assert detect_format(arr, format="stream").kind == "stream"
    assert detect_format(arr, format="brace_list").warnings == ["reader.warn_brace_list"]
    scalar = write(tmp_path, b'"x"', "s.json")

    def raises(exc, key, *args, **kw):
        with pytest.raises(exc) as ei:
            detect_format(*args, **kw)
        assert ei.value.key == key
        return ei.value

    raises(FileFormatError, "reader.root_not_container", scalar, record_path="$")
    raises(FileFormatError, "reader.format_mismatch", arr, format="keyed")
    raises(FileFormatError, "reader.format_mismatch", wrapped, format="array")
    raises(FileFormatError, "reader.format_mismatch", wrapped, record_path="$", format="array")
    raises(FileFormatError, "reader.no_record_path", write(tmp_path, b'{"a": 1}', "o.json"), format="wrapped")
    raises(FileFormatError, "reader.no_record_path", arr, format="wrapped")
    raises(UserInputError, "reader.record_path_conflict", wrapped, record_path="rows", format="array")
    raises(UserInputError, "reader.record_path_conflict", arr, record_path="$", format="jsonl")
    raises(UserInputError, "reader.bad_format", arr, format="xml")
    raises(UserInputError, "reader.bad_format", arr, format=5)
    raises(UserInputError, "reader.bad_record_path", arr, record_path=[1.5])
    raises(UserInputError, "reader.bad_record_path", arr, record_path=[True])
    raises(UserInputError, "reader.bad_record_path", arr, record_path=("a", -1))
    raises(UserInputError, "reader.bad_record_path", arr, record_path=5)
    with pytest.raises(PathSyntaxError):
        detect_format(arr, record_path="a[")


def test_format_info_round_trip_and_texts(tmp_path: Path, lang_en) -> None:
    p = write(tmp_path, b'{"meta": 1, "items": [{"a": 1}, {"a": 2}, {"a": 3}]}')
    info = detect_format(p)
    again = FormatInfo.from_dict(info.to_dict())
    assert again == info
    assert again.candidates[0] == info.candidates[0] and isinstance(again.record_path, tuple)
    assert info.record_path_text == "items"
    texts = info.warning_texts()
    assert len(texts) == 1 and "items" in texts[0] and "automatically" in texts[0]
    assert info.label == "Array inside an object"
    set_language("uz")
    assert "avtomatik" in info.warning_texts()[0] and info.label == "Obyekt ichidagi massiv"
    bl = write(tmp_path, b"{1, 2}", "b.json")
    text = detect_format(bl).warning_texts()[0]
    assert "{ {...}, {...} }" in text


# ----------------------------------------------------------------------------------
# background indexing: live counts, cancel, close
# ----------------------------------------------------------------------------------
def _many_records_file(tmp_path: Path, n: int = 6000, fmt: str = "array") -> tuple[Path, list]:
    recs = jsongen.records(200, seed=13)
    recs = (recs * (n // len(recs) + 1))[:n]
    p = tmp_path / f"many.{fmt}.json"
    jsongen.write_file(p, recs, fmt)
    return p, recs


class _Gate:
    """Progress callback that pauses the indexing thread once records are visible."""

    def __init__(self, doc: Document) -> None:
        self.doc = doc
        self.reached = threading.Event()
        self.release = threading.Event()

    def __call__(self, info) -> None:
        if not self.reached.is_set() and self.doc.record_count > 0:
            self.reached.set()
            self.release.wait(30)


@pytest.mark.parametrize("fmt", ["array", "jsonl", "wrapped"])
def test_live_records_while_indexing(tmp_path: Path, backend: str, fmt: str) -> None:
    p, recs = _many_records_file(tmp_path, fmt=fmt)
    assert p.stat().st_size > 3 * reader.FIRST_CHUNKS[0]
    with Document.open(p) as doc:
        gate = _Gate(doc)
        t0 = time.perf_counter()
        task = doc.start_index(progress=Progress(callback=gate, min_interval=0))
        try:
            assert doc.start_index() is task  # the running task is reused
            assert gate.reached.wait(30)
            first_visible = time.perf_counter() - t0
            assert first_visible < 1.0
            n = doc.record_count
            assert 0 < n < len(recs) and not doc.index_complete and not task.done
            assert "indexing" in repr(doc)
            for i in range(n):
                assert codec.dumps(doc.record(i)) == codec.dumps(recs[i])
            assert [i for i, _ in doc.iter_records(range(n))] == list(range(n))
            base = doc.record_path or ()
            assert doc.node(base + (n - 1,)) == doc.record_node(n - 1)  # no waiting for the whole index
            if fmt != "wrapped":
                root = doc.root()
                assert root.find_child(n - 1) == doc.record_node(n - 1)
                # the tree serves the first records while the index is still growing
                assert root.children(0, 3) == [doc.record_node(i) for i in range(3)]
                assert root.child(n - 1).path == (n - 1,) and not root.children_indexed
                assert not doc.index_complete
            with pytest.raises(IndexError):
                doc.record_span(len(recs) - 1)
        finally:
            gate.release.set()
        task.result(timeout=60)
        assert doc.wait_index(10) and doc.index_complete and doc.record_count == len(recs)
        assert codec.dumps(doc.record(len(recs) - 1)) == codec.dumps(recs[-1])
        assert doc.index_stats["first_record_seconds"] < 1.0 and doc.index_stats["bytes"] > 0


def test_iter_blocks_follows_a_growing_index(tmp_path: Path, backend: str) -> None:
    p, recs = _many_records_file(tmp_path, 4000)
    with Document.open(p) as doc:
        gate = _Gate(doc)
        task = doc.start_index(progress=Progress(callback=gate, min_interval=0))
        assert gate.reached.wait(30)
        got: list[int] = []
        threading.Timer(0.2, gate.release.set).start()
        for blk in doc.iter_blocks(block_bytes=50_000):
            got.extend(blk.indices)
        task.result(timeout=60)
        assert got == list(range(len(recs)))


def test_iter_blocks_builds_the_index_when_nobody_does(tmp_path: Path) -> None:
    p, recs = _many_records_file(tmp_path, 500)
    with Document.open(p) as doc:
        assert not doc.index_complete
        assert sum(len(b) for b in doc.iter_blocks()) == 500 and doc.index_complete


def test_cancel_build(tmp_path: Path, backend: str) -> None:
    p, recs = _many_records_file(tmp_path)
    with Document.open(p) as doc:
        token = CancelToken()

        def cb(info) -> None:
            if doc.record_count > 0:
                token.cancel()

        with pytest.raises(Cancelled):
            doc.build_index(progress=Progress(callback=cb, min_interval=0), cancel=token)
        assert not doc.index_complete and doc.record_count == 0 and doc.error is None
        assert index_dir_files() == []  # temporary index removed
        with pytest.raises(IndexError):
            doc.record(0)
        # a later build starts again and completes
        doc.build_index()
        assert doc.index_complete and doc.record_count == len(recs)
        assert codec.dumps(doc.record(17)) == codec.dumps(recs[17])


def test_cancel_background_task(tmp_path: Path) -> None:
    p, _ = _many_records_file(tmp_path)
    with Document.open(p) as doc:
        gate = _Gate(doc)
        task = doc.start_index(progress=Progress(callback=gate, min_interval=0))
        assert gate.reached.wait(30)
        task.cancel()
        gate.release.set()
        with pytest.raises(Cancelled):
            task.result(timeout=60)
        assert not doc.wait_index(1) and doc.record_count == 0
        task2 = doc.start_index()
        assert task2 is not task
        task2.result(timeout=60)
        assert doc.index_complete


def test_close_during_background_build(tmp_path: Path) -> None:
    p, _ = _many_records_file(tmp_path)
    doc = Document.open(p)
    gate = _Gate(doc)
    task = doc.start_index(progress=Progress(callback=gate, min_interval=0))
    assert gate.reached.wait(30)
    threading.Timer(0.1, gate.release.set).start()
    doc.close()
    assert task.done and isinstance(task.exception, Cancelled)
    assert doc.closed and "closed" in repr(doc)
    assert [n for n in index_dir_files() if n.endswith(".tmp")] == []
    with pytest.raises(ValueError):
        doc.record(0)
    with pytest.raises(ValueError):
        doc.build_index()
    doc.close()  # idempotent


def test_build_index_is_idempotent_and_waits_for_a_running_build(tmp_path: Path) -> None:
    p, recs = _many_records_file(tmp_path, 3000)
    with Document.open(p) as doc:
        gate = _Gate(doc)
        task = doc.start_index(progress=Progress(callback=gate, min_interval=0))
        assert gate.reached.wait(30)
        threading.Timer(0.2, gate.release.set).start()
        doc.build_index()  # waits for the background build instead of starting another
        assert doc.index_complete and task.done and doc.record_count == 3000
        stats = doc.index_stats
        doc.build_index()
        assert doc.index_stats is stats


# ----------------------------------------------------------------------------------
# changes, reopen, cache
# ----------------------------------------------------------------------------------
def test_check_changed_and_reopen(tmp_path: Path) -> None:
    p = write(tmp_path, b'[{"a": 1}, {"a": 2}]')
    doc = Document.open(p)
    doc.build_index()
    assert not doc.check_changed()
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert doc.check_changed()
    p.write_bytes(b'[{"a": 1}, {"a": 2}, {"a": 3}]')
    assert doc.check_changed()
    new = doc.reopen()
    try:
        assert doc.closed and not new.check_changed()
        new.build_index()
        assert new.record_count == 3 and new.record(2) == {"a": 3}
    finally:
        new.close()
    q = write(tmp_path, b"[1]", "gone.json")
    with Document.open(q) as d2:
        q.unlink()
        assert d2.check_changed()


def test_file_truncated_after_open_fails_cleanly(tmp_path: Path) -> None:
    from jsonreshape.errors import FileChangedError

    p, recs = _many_records_file(tmp_path, 2000)
    with Document.open(p) as doc:
        with open(p, "r+b") as f:
            f.truncate(p.stat().st_size // 2)
        with pytest.raises(FileChangedError):
            doc.build_index()
        assert not doc.index_complete and doc.record_count == 0 and doc.check_changed()
        assert index_dir_files() == []
    q, _ = _many_records_file(tmp_path, 50)
    with Document.open(q) as doc:
        doc.build_index()
        last = doc.record_count - 1
        with open(q, "r+b") as f:
            f.truncate(doc.record_span(last)[0])
        with pytest.raises(FileChangedError):
            doc.record(last)
        # a handle opened after the change is checked against the version opened: the
        # records that are still there are not served from a changed file either
        with pytest.raises(FileChangedError):
            doc.record(0)
        assert doc.check_changed()


def test_same_size_content_change_is_detected(tmp_path: Path) -> None:
    p = write(tmp_path, b'[{"a": 1}]')
    with Document.open(p) as doc:
        st = p.stat()
        p.write_bytes(b'[{"a": 2}]')
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # same size and mtime, other content
        assert doc.check_changed()


def test_reopen_keeps_forced_options(tmp_path: Path) -> None:
    p = write(tmp_path, b'{"m": [1, 2], "rows": [{"a": 1}]}')
    with Document.open(p, record_path="rows") as doc:
        assert doc.info.record_path == ("rows",)
        new = doc.reopen()
        assert new.info.record_path == ("rows",) and not new.info.auto
        new.close()


def test_cache_reuse_use_cache_false_and_eviction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p, recs = _many_records_file(tmp_path, 300)
    with Document.open(p) as doc:
        doc.build_index()
        path1 = doc.index_path
        assert path1 is not None and path1.exists() and not doc.index_from_cache
    with Document.open(p) as doc:
        assert doc.index_complete and doc.index_from_cache and doc.index_path == path1
    with Document.open(p, use_cache=False) as doc:
        assert not doc.index_complete
        doc.build_index()
        assert not doc.index_from_cache and doc.record_count == 300
    # eviction: a tiny cache limit keeps only the index in use
    q, _ = _many_records_file(tmp_path, 100, "jsonl")
    tiny = Settings(cache_limit_bytes=1)
    with Document.open(q, settings=tiny) as doc:
        doc.build_index()
        assert doc.index_path.exists()
        assert not path1.exists(), "LRU eviction removes the unused index"
    # a different kind/record path is a different cache entry
    r = write(tmp_path, b'{"meta": {"t": [1, 2]}, "data": [{"a": 1}, {"a": 2}, {"a": 3}]}', "w.json")
    with Document.open(r) as d1, Document.open(r, record_path="meta.t") as d2:
        d1.build_index()
        d2.build_index()
        assert d1.index_path != d2.index_path and d2.record_count == 2 and d1.record_count == 3


def test_cached_index_with_mismatching_metadata_is_ignored(tmp_path: Path) -> None:
    p, _ = _many_records_file(tmp_path, 50)
    with Document.open(p) as doc:
        doc.build_index()
        key = doc._main_key()
    with Document.open(p) as doc:
        assert doc.index_from_cache
    other = write(tmp_path, b"[1, 2, 3]", "other.json")
    with Document.open(other) as doc:  # a foreign index under this file's key is not trusted
        doc.build_index()
        src = doc.index_path
    found = find_index(key)
    assert found is not None
    target = found.path
    found.close()
    target.write_bytes(src.read_bytes())
    with Document.open(p) as doc:
        assert not doc.index_complete


# ----------------------------------------------------------------------------------
# record API
# ----------------------------------------------------------------------------------
def test_record_api(tmp_path: Path, backend: str) -> None:
    recs = jsongen.records(20, seed=31)
    p = tmp_path / "k.json"
    data = jsongen.write_file(p, recs, "keyed", style="pretty2")
    with Document.open(p, settings=Settings(large_node_bytes=400)) as doc:
        doc.build_index()
        assert doc.member_records and doc.kind == "keyed" and doc.record_path is None
        assert doc.size == p.stat().st_size == doc.data_size and doc.data_path == p.resolve()
        for i in range(doc.record_count):
            s, e = doc.record_span(i)
            assert data[s:s + 1] == b'"' and doc.record_key(i) == f"id{i}"
            vs, ve = doc.record_value_span(i)
            assert ve == e and codec.loads(data[vs:ve]) == codec.loads(codec.dumps(recs[i]))
            assert doc.is_huge(i) == (e - s > 400)
            if doc.is_huge(i):
                with pytest.raises(TooLargeError):
                    doc.record_bytes(i)
                with pytest.raises(TooLargeError):
                    doc.record(i)
            else:
                assert doc.record_bytes(i) == data[s:e]
                key, vo = doc.split_member(doc.record_bytes(i))
                assert key == f"id{i}" and doc.record_bytes(i)[vo:] == data[vs:ve]
        assert any(doc.is_huge(i) for i in range(20)) and not all(doc.is_huge(i) for i in range(20))
        for bad in (-1, 20, 10**9):
            with pytest.raises(IndexError):
                doc.record_span(bad)
        with pytest.raises(ValueError):
            doc.split_member(b'[1, 2]')
        assert reader.split_member(b' "a\\u0062" :  [1]') == ("ab", 14)


def test_non_member_records_have_no_key(tmp_path: Path) -> None:
    p = write(tmp_path, b'["a", "b"]')
    with Document.open(p) as doc:
        doc.build_index()
        assert not doc.member_records and doc.record_key(0) is None
        assert doc.record_value_span(1) == doc.record_span(1) == (6, 9)


def test_flags(tmp_path: Path) -> None:
    p = write(tmp_path, '["plain", "тош"]'.encode("utf-8"))
    with Document.open(p) as doc:
        doc.build_index()
        assert doc.flags == FLAG_NON_ASCII
    q = write(tmp_path, b'["a\\n", "\\u0442"]', "e.json")
    with Document.open(q) as doc:
        doc.build_index()
        assert doc.flags == FLAG_ESCAPES | FLAG_U_ESCAPES


def test_gzip_document_paths(tmp_path: Path) -> None:
    raw = jsongen.build_document(jsongen.records(5, seed=2), "array")
    p = tmp_path / "a.json.gz"
    p.write_bytes(gzip.compress(raw))
    with Document.open(p) as doc:
        assert doc.source_path == p.resolve() and doc.data_path != doc.source_path
        assert doc.size == p.stat().st_size and doc.data_size == len(raw)
        assert doc.data_path.read_bytes() == raw


# ----------------------------------------------------------------------------------
# blocks and record iteration
# ----------------------------------------------------------------------------------
class FakeRecordSet:
    """Sequence-like source (``__len__``/``__getitem__``) with ``iter_chunks``."""

    def __init__(self, indices: list[int], chunks: bool) -> None:
        self.indices = indices
        if chunks:
            self.iter_chunks = self._iter_chunks

    def __len__(self) -> int:
        return len(self.indices)

    def __getitem__(self, i: int) -> int:
        return self.indices[i]

    def _iter_chunks(self, size: int = 7):
        for k in range(0, len(self.indices), size):
            yield array("Q", self.indices[k:k + size])


def test_iter_blocks_sources_and_layout(tmp_path: Path, backend: str) -> None:
    recs = jsongen.records(60, seed=17)
    p = tmp_path / "a.json"
    data = jsongen.write_file(p, recs, "array", style="pretty2")
    with Document.open(p) as doc:
        doc.build_index()
        n = doc.record_count
        spans = [doc.record_span(i) for i in range(n)]
        rng = random.Random(3)
        perm = rng.sample(range(n), n)
        sources = [None, range(n), range(5, 40), range(0, n, 3), range(n - 1, -1, -1), list(range(n)), perm,
                   (i for i in perm), FakeRecordSet(perm, chunks=True), FakeRecordSet(perm, chunks=False),
                   array("Q", perm), range(0), []]
        for source in sources:
            expected = list(range(n)) if source is None else (
                list(source.indices) if isinstance(source, FakeRecordSet) else None)
            got = []
            for blk in doc.iter_blocks(source, block_bytes=2048):
                assert isinstance(blk, Block) and not blk.huge and len(blk) == len(blk.indices)
                assert isinstance(blk.indices, array) and blk.indices.typecode == "Q"
                if blk.contiguous:
                    assert blk.base == spans[blk.indices[0]][0] and len(blk.data) <= max(2048, len(blk.data))
                    assert len(blk.data) <= 2048 or len(blk) == 1
                    assert data[blk.base:blk.base + len(blk.data)] == blk.data
                else:
                    assert blk.base == -1 and len(blk.data) <= 2048
                    assert blk.data == b"\n".join(blk.record_bytes(k) for k in range(len(blk)))
                for k, i in enumerate(blk.indices):
                    assert blk.record_bytes(k) == data[spans[i][0]:spans[i][1]]
                got.extend(blk.indices)
            if expected is not None:
                assert got == expected
        assert [list(b.indices) for b in doc.iter_blocks([3, 4, 5, 9], block_bytes=10**6)] == [[3, 4, 5, 9]]
        assert [list(b.indices) for b in doc.iter_blocks([9, 3], block_bytes=10**6)] == [[9, 3]]
        for bad in ([n], [0, n + 5], range(n - 2, n + 1), [-1]):
            with pytest.raises(IndexError):
                list(doc.iter_blocks(bad))


def test_iter_blocks_huge_records_and_progress(tmp_path: Path) -> None:
    recs = [{"i": i, "pad": "x" * (2000 if i % 4 == 0 else 10)} for i in range(16)]
    p = tmp_path / "h.json"
    data = jsongen.write_file(p, recs, "array")
    with Document.open(p, settings=Settings(large_node_bytes=1000)) as doc:
        doc.build_index()
        for source in (None, list(range(16)), list(range(15, -1, -1))):
            progress = Progress()
            seen = []
            for blk in doc.iter_blocks(source, block_bytes=1 << 20, progress=progress):
                if blk.huge:
                    i = int(blk.indices[0])
                    assert i % 4 == 0 and len(blk) == 1 and blk.data == b"" and not blk.contiguous
                    assert blk.span(0) == doc.record_span(i)
                    with pytest.raises(TooLargeError):
                        blk.record_bytes(0)
                else:
                    assert all(i % 4 for i in blk.indices)
                seen.extend(blk.indices)
            assert sorted(seen) == list(range(16)) and progress.finished
        assert progress.done > 0
        assert [i for i, _ in doc.iter_records()] == [i for i in range(16) if i % 4]
        values = dict(doc.iter_records(include_huge=True))
        assert values[0] is None and values[1] == recs[1]
        raw = dict(doc.iter_record_bytes(include_huge=True))
        assert raw[4] is None and raw[5] == data[slice(*doc.record_span(5))]
        assert len(dict(doc.iter_record_bytes())) == 12


def test_iter_records_keyed_and_progress(tmp_path: Path) -> None:
    recs = jsongen.records(30, seed=5)
    p = tmp_path / "k.json"
    jsongen.write_file(p, recs, "keyed")
    with Document.open(p) as doc:
        progress = Progress()
        out = list(doc.iter_records(progress=progress))
        assert [i for i, _ in out] == list(range(30))
        assert all(codec.dumps(v) == codec.dumps(r) for (_, v), r in zip(out, recs))
        assert progress.total == doc.data_size and progress.finished
        sel = list(doc.iter_records([29, 0, 7]))
        assert [i for i, _ in sel] == [29, 0, 7] and codec.dumps(sel[0][1]) == codec.dumps(recs[29])


# ----------------------------------------------------------------------------------
# wrapper
# ----------------------------------------------------------------------------------
def test_wrapper_parts_and_spans(tmp_path: Path) -> None:
    recs = jsongen.records(10, seed=8)
    p = tmp_path / "w.json"
    data = jsongen.write_file(p, recs, "wrapped", style="pretty2", bom=True, wrapper_path=("a", "b"))
    with Document.open(p) as doc:
        (ps, pe), (ss, se) = doc.wrapper_spans()  # builds the index when needed
        assert doc.index_complete and doc.info.data_start == 3
        raw = b"\xef\xbb\xbf" + data
        cs, ce = doc.container_span
        assert (ps, pe, ss, se) == (3, cs, ce, len(raw))
        prefix, suffix = doc.wrapper_parts()
        assert prefix == raw[3:cs] and suffix == raw[ce:]
        assert prefix.rstrip().endswith(b'"b":') and raw[cs:cs + 1] == b"[" and raw[ce - 1:ce] == b"]"
        assert codec.loads(prefix + b"[]" + suffix) == {"meta": {"version": 1, "source": "test", "list": [1, 2, 3]},
                                                        "a": {"b": []}, "tail": {"count": 10, "ok": True}}
    with Document.open(p, settings=Settings(large_node_bytes=10)) as doc:
        with pytest.raises(TooLargeError):
            doc.wrapper_parts()
        assert doc.wrapper_spans() is not None
    arr = write(tmp_path, b"[1]", "arr.json")
    with Document.open(arr) as doc:
        assert doc.wrapper_parts() is None and doc.wrapper_spans() is None


# ----------------------------------------------------------------------------------
# thread safety
# ----------------------------------------------------------------------------------
def test_concurrent_readers(tmp_path: Path, backend: str) -> None:
    recs = jsongen.records(300, seed=19)
    p = tmp_path / "t.json"
    jsongen.write_file(p, recs, "jsonl")
    expected = [codec.dumps(r) for r in recs]
    errors: list[str] = []
    with Document.open(p) as doc:
        doc.start_index()

        def worker(seed: int) -> None:
            rng = random.Random(seed)
            try:
                doc.wait_index(60)
                for _ in range(300):
                    i = rng.randrange(300)
                    if codec.dumps(doc.record(i)) != expected[i]:
                        errors.append(f"record {i}")
                for i, v in doc.iter_records(rng.sample(range(300), 50)):
                    if codec.dumps(v) != expected[i]:
                        errors.append(f"iter {i}")
                node = doc.node((rng.randrange(300), "id"))
                node.value()
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert not errors, errors[:5]
        assert len(doc._handles) >= 8  # one shared-read handle per thread
    assert all(f.closed for f in doc._handles) or doc._handles == []


@pytest.mark.slow
def test_full_scan_does_not_grow_the_working_set(tmp_path: Path) -> None:
    import gc

    import psutil

    p = tmp_path / "tiny.json"
    with open(p, "wb") as f:
        f.write(b"[" + b",".join(b"%d" % (i % 10) for i in range(3_000_000)) + b"]")
    proc = psutil.Process()
    with Document.open(p) as doc:
        doc.build_index()
        assert doc.record_count == 3_000_000  # a 48 MB index
        gc.collect()
        rss0 = proc.memory_info().rss
        n = sum(len(b) for b in doc.iter_blocks())
        growth = proc.memory_info().rss - rss0
        assert n == 3_000_000
        # spans of sequential scans are read from the index file, not through the mapping
        assert growth < 24_000_000, f"working set grew by {growth / 1e6:.0f} MB"


@pytest.mark.slow
def test_first_record_visible_quickly_on_a_bigger_file(tmp_path: Path) -> None:
    rec = codec.dumps(jsongen.records(1, seed=1)[0])
    p = tmp_path / "big.json"
    with open(p, "wb") as f:
        f.write(b"[" + rec)
        chunk = (b"," + rec) * 2000
        while f.tell() < 40_000_000:
            f.write(chunk)
        f.write(b"]")
    t0 = time.perf_counter()
    with Document.open(p) as doc:
        t_open = time.perf_counter() - t0
        task = doc.start_index()
        t1 = time.perf_counter()
        while doc.record_count == 0 and not task.done:
            time.sleep(0.001)
        t_first = time.perf_counter() - t1
        assert doc.record(0) == codec.loads(rec)
        task.result(timeout=300)
        t_all = time.perf_counter() - t1
        mb = p.stat().st_size / 1e6
        print(f"\n[{doc.backend}] {mb:.0f} MB: open {t_open * 1000:.0f} ms, first record after "
              f"{t_first * 1000:.1f} ms, full index {t_all:.2f} s ({mb / t_all:.0f} MB/s)")
        assert t_open < 1.0 and t_first < 1.0


def test_sparse_ascending_selection_reads_only_needed_bytes(tmp_path):
    """Sparse ascending selections (e.g. schema samples) must not read the gaps."""
    from array import array

    from jsonreshape.reader import Document

    recs = [{"id": i, "pad": "x" * 200} for i in range(20000)]
    path = tmp_path / "sparse.json"
    path.write_bytes(b"[" + b",\n".join(jsongen.encode(r) for r in recs) + b"]")
    with Document.open(path) as doc:
        doc.build_index()
        picks = array("Q", range(0, 20000, 997))
        got = []
        read = 0
        for block in doc.iter_blocks(picks):
            read += len(block.data)
            for k, i in enumerate(block.indices):
                got.append((i, block.record_bytes(k)))
        assert [i for i, _ in got] == list(picks)
        assert all(codec.loads(b) == recs[i] for i, b in got)
        needed = sum(doc.record_span(i)[1] - doc.record_span(i)[0] for i in picks)
        # coalescing may read small neighbourhoods, never the multi-MB gaps
        assert read < needed * 3 + len(picks) * 64 * 1024, (read, needed)
        dense = sum(len(b.data) for b in doc.iter_blocks(array("Q", range(0, 2000))))
        assert dense >= sum(doc.record_span(i)[1] - doc.record_span(i)[0] for i in range(2000))
