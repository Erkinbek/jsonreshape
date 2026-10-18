"""Lazy node tree (AGENT_PROMPT §3 "Ichki konteynerlarni talab bo'yicha indekslash", §4.1,
§14 test 14; docs/ARCHITECTURE.md §5.3 ``Node``).

* navigation, kinds, keys, values, ``scalar_text``, parent helpers, path syntaxes and
  dict semantics (last duplicate key wins, canonical index strings on arrays);
* the records of array/keyed/jsonl/stream/brace_list documents are the root's children
  (main index), wrapped documents expand their wrapper without rescanning the record array;
* a 1,000,000-element array inside a single-object document is opened through the
  on-disk child index without filling memory, and the child index is reused from cache.
"""

from __future__ import annotations

import gc
import os
import threading
import time
from pathlib import Path

import psutil
import pytest

import jsongen
from jsonreshape import codec, reader
from jsonreshape.errors import TooLargeError, UserInputError
from jsonreshape.index import FLAG_NODE, IndexReader
from jsonreshape.reader import Document, Node
from jsonreshape.settings import Settings

ELL = "\N{HORIZONTAL ELLIPSIS}"


@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    yield name
    reader.set_scanner_backend(old)


class CountingScanner:
    """Wraps a core Scanner class and counts the bytes fed to every instance."""

    def __init__(self, real) -> None:
        self.real = real
        self.fed = 0
        self.instances = 0

    def __call__(self, *args, **kwargs):
        counter = self
        counter.instances += 1
        inner = self.real(*args, **kwargs)

        class _Proxy:
            def feed(self, chunk):
                counter.fed += len(chunk)
                return inner.feed(chunk)

            def __getattr__(self, name):
                return getattr(inner, name)

        return _Proxy()


NESTED = (
    '{\n'
    '  "name": "Toshkent",\n'
    '  "count": 3,\n'
    '  "price": 1.10,\n'
    '  "big": 12345678901234567890,\n'
    '  "flag": true, "none": null,\n'
    '  "list": [1, "two", {"three": 3}, [4], []],\n'
    '  "obj": {"a": {"b": {"c": "deep"}}, "empty": {}},\n'
    '  "esc\\"key": "v",\n'
    '  "k\\u00e9y": "escaped key",\n'
    '  "long": "' + "x" * 1000 + '",\n'
    '  "dup": 1,\n'
    '  "dup": {"z": 2}\n'
    '}\n'
)


@pytest.fixture
def nested_doc(tmp_path: Path, backend: str):
    p = tmp_path / "nested.json"
    p.write_bytes(NESTED.encode("utf-8"))
    doc = Document.open(p, format="single")  # auto detection would pick "list" (rule c)
    assert doc.info.kind == "single"
    doc.build_index()
    yield doc
    doc.close()


def test_navigation_kinds_keys_values(nested_doc: Document) -> None:
    doc = nested_doc
    expected = codec.loads(NESTED.encode("utf-8"))
    root = doc.root()
    assert root.path == () and root.key is None and root.kind == "object" and root.is_container
    assert root.value() == expected
    keys = ["name", "count", "price", "big", "flag", "none", "list", "obj", 'esc"key', "kéy", "long", "dup",
            "dup"]
    assert root.child_count() == len(keys)
    kids = root.children()
    assert [c.key for c in kids] == keys
    assert [c.path for c in kids] == [(k,) for k in keys]
    assert [c.kind for c in kids] == ["string", "number", "number", "number", "bool", "null", "array", "object",
                                      "string", "string", "string", "number", "object"]
    assert codec.dumps(kids[2].value()) == b"1.10" and kids[3].value() == 12345678901234567890
    assert kids[11].value() == 1 and kids[12].value() == {"z": 2}
    assert root.children(2, 4) == kids[2:4] and root.children(20) == [] and root.child(6) == kids[6]
    with pytest.raises(IndexError):
        root.child(13)
    lst = root.child(6)
    assert lst.child_count() == 5 and [c.key for c in lst.children()] == [0, 1, 2, 3, 4]
    assert [c.kind for c in lst.children()] == ["number", "string", "object", "array", "array"]
    assert lst.child(2).child(0).path == ("list", 2, "three") and lst.child(4).child_count() == 0
    assert kids[0].child_count() == 0 and kids[0].children() == []
    # raw bytes / sizes
    assert kids[1].raw() == b"3" and kids[1].size == 1
    assert doc.data_path.read_bytes()[kids[7].start:kids[7].end] == kids[7].raw()


def test_node_paths_and_dict_semantics(nested_doc: Document) -> None:
    doc = nested_doc
    assert doc.node("obj.a.b.c").value() == "deep"
    assert doc.node("$.obj.a").value() == {"b": {"c": "deep"}}
    assert doc.node("/list/2/three").value() == 3
    assert doc.node(["list", 2, "three"]).value() == 3
    assert doc.node(("list", "3", "0")).value() == 4          # canonical index strings on arrays
    assert doc.node('["esc\\"key"]').value() == "v"
    assert doc.node(("kéy",)).value() == "escaped key"   # key written with a \\u escape
    assert doc.node("dup").value() == {"z": 2}                 # last duplicate wins (dict semantics)
    assert doc.node("dup.z").value() == 2
    assert doc.node("").path == () and doc.node("$").kind == "object"
    for bad in ("list[9]", "name.x", ("obj", 0), ("list", "01"), ("list", "-1"), "nope", "obj.a.b.c.d"):
        with pytest.raises(UserInputError) as ei:
            doc.node(bad)
        assert ei.value.key == "reader.path_not_found"


def test_scalar_text_and_helpers(nested_doc: Document) -> None:
    doc = nested_doc
    assert doc.node("name").scalar_text() == '"Toshkent"'
    assert doc.node("price").scalar_text() == "1.10"
    assert doc.node("big").scalar_text() == "12345678901234567890"
    assert doc.node("flag").scalar_text() == "true" and doc.node("none").scalar_text() == "null"
    long = doc.node("long").scalar_text(max_chars=50)
    assert len(long) == 50 and long == '"' + "x" * 48 + ELL
    assert len(doc.node("long").scalar_text()) == 200
    assert doc.node("obj").scalar_text() == '{"a": {"b": {"c": "deep"}}, "empty": {}}'
    assert doc.root().scalar_text(30).endswith(ELL) and "\n" not in doc.root().scalar_text(30)
    n = doc.node("obj.a.b")
    assert n.parent_path == ("obj", "a") and n.parent() == doc.node("obj.a") and n.depth == 3
    assert doc.root().parent() is None and doc.root().parent_path is None
    assert n.path_text == "obj.a.b" and "obj.a.b" in repr(n)
    assert {doc.node("obj"), doc.node("obj")} == {doc.node("obj")}


def test_large_node_value_is_refused(tmp_path: Path, backend: str) -> None:
    value = {"small": [1, 2], "big": ["x" * 50 for _ in range(100)]}
    p = tmp_path / "big.json"
    p.write_bytes(codec.dumps(value))
    settings = Settings(large_node_bytes=1000)
    with Document.open(p, settings=settings, format="single") as doc:
        doc.build_index()
        big = doc.node("big")
        assert big.is_large and big.size > 1000
        with pytest.raises(TooLargeError):
            big.value()
        with pytest.raises(TooLargeError):
            doc.root().value()
        assert doc.node("small").value() == [1, 2]
        assert big.scalar_text(20) == '["' + "x" * 17 + ELL
        # a large container keeps its child spans on disk (never parsed)
        assert big.child_count() == 100 and big.child(99).value() == "x" * 50
        assert big.start in doc._node_readers


def test_many_children_spill_to_disk(tmp_path: Path, backend: str) -> None:
    value = {"items": list(range(25_000)), "obj": {f"k{i}": i for i in range(25_000)}}
    p = tmp_path / "many.json"
    p.write_bytes(codec.dumps(value))
    settings = Settings(large_node_children=10_000)
    with Document.open(p, settings=settings, format="single") as doc:
        doc.build_index()
        items, obj = doc.node("items"), doc.node("obj")
        assert items.child_count() == 25_000 and obj.child_count() == 25_000
        assert items.start in doc._node_readers and obj.start in doc._node_readers
        reader_ = doc._node_readers[obj.start]
        assert reader_.flags & FLAG_NODE and reader_.meta["kind"] == "node"
        assert items.child(24_999).value() == 24_999
        assert obj.child(12_345).key == "k12345" and obj.child(12_345).value() == 12_345
        assert doc.node("obj.k24999").value() == 24_999
        assert doc.node(("items", "17")).value() == 17
        small = doc.root()
        assert small.child_count() == 2 and small.start not in doc._node_readers


# ----------------------------------------------------------------------------------
# records as children
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt, kw", [("jsonl", {}), ("stream", {"style": "pretty2"}), ("brace_list", {}),
                                     ("array", {"style": "pretty2"})])
def test_record_children(tmp_path: Path, backend: str, fmt: str, kw: dict) -> None:
    recs = jsongen.records(15, seed=3)
    p = tmp_path / "r.json"
    jsongen.write_file(p, recs, fmt, **kw)
    with Document.open(p) as doc:
        root = doc.root()
        assert root.path == () and root.kind == "array"
        assert root.is_virtual == (fmt != "array")
        assert root.child_count() == 15 and doc.index_complete
        kids = root.children()
        assert [c.path for c in kids] == [(i,) for i in range(15)]
        assert [c.key for c in kids] == list(range(15))
        assert all(codec.dumps(c.value()) == codec.dumps(r) for c, r in zip(kids, recs))
        assert kids[4] == doc.record_node(4) and (kids[4].start, kids[4].end) == doc.record_span(4)
        assert doc.node((7, "name")).value() == recs[7]["name"]
        assert doc.node("[7].id").value() == 7 and doc.node(("7",)).value() == recs[7]
        assert codec.dumps(root.value()) == codec.dumps(recs)
        if fmt != "array":
            assert root.scalar_text() == "[" + ELL + "]"
        with pytest.raises(UserInputError):
            doc.node((15,))


def test_keyed_root_children_are_the_records(tmp_path: Path, backend: str) -> None:
    recs = jsongen.records(12, seed=6)
    keys = [f"id{i}" for i in range(12)]
    keys[5] = "Тошкент"
    p = tmp_path / "k.json"
    jsongen.write_file(p, recs, "keyed", keys=keys, style="escaped")
    with Document.open(p) as doc:
        assert doc.info.kind == "keyed"
        root = doc.root()
        assert root.kind == "object" and root.child_count() == 12
        kids = root.children()
        assert [c.key for c in kids] == keys and [c.path for c in kids] == [(k,) for k in keys]
        assert codec.dumps(kids[3].value()) == codec.dumps(recs[3])
        assert doc.node(("Тошкент",)).value() == recs[5]
        assert doc.node(("Тошкент", "id")).value() == 5
        assert doc.node("id11.name").value() == recs[11]["name"]
        assert doc.record_node(5) == kids[5]
        with pytest.raises(UserInputError):
            doc.node(("nope",))
        with pytest.raises(UserInputError):
            doc.node((0,))


def test_keyed_duplicate_keys_resolve_to_the_last(tmp_path: Path, backend: str) -> None:
    p = tmp_path / "dup.json"
    p.write_bytes(b'{"a": {"v": 1}, "b": {"v": 2}, "a": {"v": 3}}')
    with Document.open(p) as doc:
        assert doc.info.kind == "keyed"
        assert doc.node("a").value() == {"v": 3} and doc.node("a.v").value() == 3
        assert [c.key for c in doc.root().children()] == ["a", "b", "a"]


# ----------------------------------------------------------------------------------
# wrapped documents: ancestors without rescanning the record array
# ----------------------------------------------------------------------------------
def test_wrapped_tree(tmp_path: Path, backend: str) -> None:
    recs = jsongen.records(20, seed=9)
    p = tmp_path / "w.json"
    jsongen.write_file(p, recs, "wrapped", style="pretty2", wrapper_path=("a", "b"))
    with Document.open(p) as doc:
        assert doc.info.record_path == ("a", "b")
        root = doc.root()
        assert root.kind == "object" and root.path == ()
        kids = root.children()
        assert [c.key for c in kids] == ["meta", "a", "tail"]
        assert kids[0].value() == {"version": 1, "source": "test", "list": [1, 2, 3]}
        assert kids[2].value() == {"count": 20, "ok": True}
        a = kids[1]
        assert a.child_count() == 1
        b = a.child(0)
        assert b.path == ("a", "b") and b.kind == "array" and (b.start, b.end) == doc.container_span
        assert b.child_count() == 20
        assert [c.path for c in b.children(0, 3)] == [("a", "b", 0), ("a", "b", 1), ("a", "b", 2)]
        assert codec.dumps(b.child(19).value()) == codec.dumps(recs[19])
        assert doc.node("a.b[3].id").value() == 3
        assert doc.node(("a", "b")) == b
        assert doc.node("meta.list[2]").value() == 3
        assert doc.record_node(4).path == ("a", "b", 4)
        assert codec.dumps(doc.node("a").value()) == codec.dumps({"b": recs})


def test_wrapper_ancestors_are_expanded_without_rescanning_records(tmp_path: Path, backend: str) -> None:
    recs = jsongen.records(400, seed=10)
    p = tmp_path / "big.json"
    jsongen.write_file(p, recs * 25, "wrapped", wrapper_path=("result", "items"))  # ~2-3 MB of records
    with Document.open(p) as doc:
        doc.build_index()
        cs, ce = doc.container_span
        prefix, suffix = doc.wrapper_parts()
        wrapper_bytes = len(prefix) + len(suffix)
        assert ce - cs > 1_000_000 > 100 * wrapper_bytes
        counting = CountingScanner(doc._scanner_cls)
        doc._scanner_cls = counting
        root = doc.root()
        assert [c.key for c in root.children()] == ["meta", "result", "tail"]
        result = doc.node("result")
        items = result.child(0)
        assert items.path == ("result", "items") and items.child_count() == 10_000
        assert doc.node("tail.count").value() == 10_000
        # only wrapper bytes (+ the "[]" placeholders) went through the scanner: the root,
        # "result" and "tail" scans each read their part of the wrapper, never the records
        assert counting.instances == 3
        assert counting.fed <= 3 * (wrapper_bytes + 2)
        assert counting.fed < (ce - cs) / 100
        assert doc.bytes_scanned_for_nodes == counting.fed


def test_wrapped_object_container_children(tmp_path: Path, backend: str) -> None:
    p = tmp_path / "wo.json"
    p.write_bytes(b'{"meta": [1], "users": {"u1": {"n": 1}, "u2": {"n": 2}}, "z": 0}')
    with Document.open(p, record_path="users") as doc:
        root = doc.root()
        assert [c.key for c in root.children()] == ["meta", "users", "z"]
        users = root.child(1)
        assert users.kind == "object" and users.child_count() == 2
        assert [c.path for c in users.children()] == [("users", "u1"), ("users", "u2")]
        assert doc.node("users.u2.n").value() == 2 and doc.record_node(0).key == "u1"


def test_truncated_wrapped_document_tree(tmp_path: Path, backend: str) -> None:
    recs = jsongen.records(10, seed=12)
    data = jsongen.build_document(recs, "wrapped")
    p = tmp_path / "cut.json"
    cut = data.index(b',"tail"') - 30
    p.write_bytes(data[:cut])
    with Document.open(p, record_path="data") as doc:
        doc.build_index()
        assert doc.partial and doc.container_span is None
        n = doc.record_count
        assert 0 < n < 10
        root = doc.root()
        kids = root.children()
        assert [c.key for c in kids] == ["meta", "data"]
        assert kids[1].child_count() == n
        assert codec.dumps(doc.node(("data", n - 1)).value()) == codec.dumps(recs[n - 1])


# ----------------------------------------------------------------------------------
# §14 test 14: a 1,000,000-element array through the on-disk child index
# ----------------------------------------------------------------------------------
def _million_file(path: Path) -> None:
    with open(path, "wb") as f:
        f.write(b'{"meta": {"n": 1000000, "note": "single object"}, "items": [')
        f.write(b",".join(b"%d" % i for i in range(1_000_000)))
        f.write(b'], "tail": true}')


class _PeakRSS:
    def __init__(self) -> None:
        self.proc = psutil.Process()
        self.peak = self.proc.memory_info().rss
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, self.proc.memory_info().rss)
            time.sleep(0.005)

    def __enter__(self) -> _PeakRSS:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()
        self.peak = max(self.peak, self.proc.memory_info().rss)


def test_million_children_node_uses_the_on_disk_index(tmp_path: Path, backend: str) -> None:
    p = tmp_path / "million.json"
    _million_file(p)
    gc.collect()
    proc = psutil.Process()
    rss0 = proc.memory_info().rss
    with Document.open(p, format="single") as doc:
        doc.build_index()
        items = doc.node("items")
        assert items.kind == "array" and not items.children_indexed
        with _PeakRSS() as peak:
            t0 = time.perf_counter()
            count = items.child_count()
            t_index = time.perf_counter() - t0
            t0 = time.perf_counter()
            window = items.children(500_000, 500_050)
            last = items.child(999_999)
            t_window = time.perf_counter() - t0
        assert count == 1_000_000 and items.children_indexed
        assert [c.value() for c in window] == list(range(500_000, 500_050))
        assert [c.path for c in window[:2]] == [("items", 500_000), ("items", 500_001)]
        assert last.value() == 999_999 and last.key == 999_999
        assert doc.node("items[777777]").value() == 777_777
        assert doc.node(("items", "123456")).value() == 123_456
        # the child spans live in an on-disk index (FLAG_NODE), memory-mapped
        node_index = doc._node_readers[items.start]
        assert isinstance(node_index, IndexReader) and node_index.flags & FLAG_NODE
        assert node_index.count == 1_000_000 and node_index.path.stat().st_size > 16_000_000
        growth = peak.peak - rss0
        print(f"\n[{backend}] 1M children: indexed in {t_index * 1000:.0f} ms, 50-child window + last child "
              f"in {t_window * 1000:.2f} ms, peak RSS growth {growth / 1e6:.1f} MB")
        assert growth < 50_000_000, f"RSS grew by {growth / 1e6:.0f} MB (must stay well under 100 MB)"
        assert t_window < 0.2

    # the child index is reused from cache: no scanning at all
    with Document.open(p, format="single") as doc:
        assert doc.index_from_cache
        counting = CountingScanner(doc._scanner_cls)
        doc._scanner_cls = counting
        items = doc.node("items")
        t0 = time.perf_counter()
        assert items.child_count() == 1_000_000
        t_cached = time.perf_counter() - t0
        assert counting.fed == 0 and doc.bytes_scanned_for_nodes == 0
        assert items.child(42).value() == 42
        print(f"[{backend}] 1M children reopened from the cached child index in {t_cached * 1000:.1f} ms")
        assert t_cached < 0.2


def test_node_api_on_closed_document(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_bytes(b'{"a": [1, 2]}')
    doc = Document.open(p)
    node = doc.node("a")
    doc.close()
    with pytest.raises(ValueError):
        node.value()
    with pytest.raises(ValueError):
        doc.root()
    assert isinstance(node, Node)
    assert os.path.exists(p)
