"""Node.find_child_index: the child index find_child resolves a path segment to.

* objects: the LAST member with the key (dict semantics), escaped keys, missing keys;
* arrays: int and canonical-decimal string segments, out of range, non-canonical;
* the node holding the records (array / keyed / jsonl / brace_list / stream roots, the
  record container of wrapped documents): record numbers, keyed duplicates;
* scalars and wrong segment types give None;
* ``find_child(seg)`` is exactly ``child(find_child_index(seg))`` (records: the record node)
  -- also for on-disk child lists (more than ``large_node_children`` children).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jsonreshape.reader import Document
from jsonreshape.settings import Settings


def _open(path: Path, data: bytes, **kw) -> Document:
    path.write_bytes(data)
    doc = Document.open(path, **kw)
    doc.build_index()
    return doc


def _check_consistent(node, segs) -> None:
    """find_child(seg) == child(find_child_index(seg)) for every segment."""
    for seg in segs:
        idx = node.find_child_index(seg)
        found = node.find_child(seg)
        if idx is None:
            assert found is None, seg
        else:
            expected = node.doc.record_node(idx) if node.role in ("records", "virtual") else node.child(idx)
            assert found == expected, seg
            assert found.start == expected.start and found.end == expected.end


def test_object_members_last_duplicate_wins(tmp_path: Path) -> None:
    data = b'{"a": 1, "b": {"x": [10, 20, 30]}, "a": 2, "c\\u0041": 3, "": 4, "a b": 5}'
    with _open(tmp_path / "o.json", data, format="single") as doc:
        root = doc.root()
        assert root.kind == "object"
        assert root.find_child_index("a") == 2          # the last "a"
        assert root.child(2).value() == 2
        assert root.find_child_index("b") == 1
        assert root.find_child_index("cA") == 3         # escaped key decoded
        assert root.find_child_index("") == 4
        assert root.find_child_index("a b") == 5
        assert root.find_child_index("missing") is None
        assert root.find_child_index(0) is None         # int segment on an object
        _check_consistent(root, ["a", "b", "cA", "", "a b", "missing", 0, "0"])


def test_array_elements(tmp_path: Path) -> None:
    with _open(tmp_path / "a.json", b'{"x": [10, 20, 30], "s": "text", "n": null}', format="single") as doc:
        arr = doc.node(("x",))
        assert arr.find_child_index(0) == 0
        assert arr.find_child_index(2) == 2
        assert arr.find_child_index("1") == 1           # canonical decimal string
        assert arr.find_child_index("01") is None       # not canonical
        assert arr.find_child_index(3) is None
        assert arr.find_child_index(-1) is None
        assert arr.find_child_index(True) is None       # bool is not an index
        assert arr.find_child_index("x") is None
        _check_consistent(arr, [0, 1, 2, 3, "1", "01", -1, "x"])
        scalar = doc.node(("s",))
        assert scalar.find_child_index(0) is None
        assert scalar.find_child_index("a") is None
        assert doc.node(("n",)).find_child_index("a") is None


@pytest.mark.parametrize("fmt", ["array", "jsonl", "brace_list", "stream"])
def test_records_node_gives_record_numbers(tmp_path: Path, fmt: str) -> None:
    recs = [{"id": i, "v": [i, i + 1]} for i in range(25)]
    if fmt == "array":
        data = json.dumps(recs).encode()
    elif fmt == "jsonl":
        data = b"".join(json.dumps(r).encode() + b"\n" for r in recs)
    elif fmt == "brace_list":
        data = b"{" + b",".join(json.dumps(r).encode() for r in recs) + b"}"
    else:
        data = b"\n".join(json.dumps(r, indent=1).encode() for r in recs) + b"\n"
    with _open(tmp_path / f"r.{fmt}.json", data, format=fmt) as doc:
        assert doc.kind == fmt
        root = doc.root()
        assert root.find_child_index(0) == 0
        assert root.find_child_index(24) == 24
        assert root.find_child_index("7") == 7
        assert root.find_child_index(25) is None
        assert root.find_child_index("id") is None
        _check_consistent(root, [0, 7, "7", 24, 25, "id", -1])
        rec = doc.record_node(7)
        assert rec.find_child_index("v") == 1
        assert rec.find_child("v").child(rec.find_child("v").find_child_index(1)).value() == 8


def test_keyed_records_and_duplicate_keys(tmp_path: Path) -> None:
    data = b'{"k1": {"a": 1}, "k2": {"a": 2}, "k1": {"a": 3}, "k3": {"a": 4}}'
    with _open(tmp_path / "keyed.json", data) as doc:
        assert doc.kind == "keyed"
        root = doc.root()
        assert root.find_child_index("k1") == 2         # the last duplicate
        assert root.find_child_index("k2") == 1
        assert root.find_child_index("k3") == 3
        assert root.find_child_index("nope") is None
        assert root.find_child_index(0) is None         # member records need a key
        assert doc.record_node(2).value() == {"a": 3}
        _check_consistent(root, ["k1", "k2", "k3", "nope", 0])


def test_wrapped_container_and_wrapper_members(tmp_path: Path) -> None:
    recs = [{"id": i} for i in range(10)]
    data = (b'{"meta": {"v": 1}, "data": ' + json.dumps(recs).encode() + b', "tail": {"n": 10}}')
    with _open(tmp_path / "w.json", data, record_path="data") as doc:
        assert doc.kind == "wrapped"
        root = doc.root()
        assert root.find_child_index("meta") == 0
        assert root.find_child_index("data") == 1
        assert root.find_child_index("tail") == 2
        container = root.child(1)
        assert container.role == "records"
        assert container.find_child_index(9) == 9
        assert container.find_child_index("3") == 3
        assert container.find_child_index(10) is None
        _check_consistent(root, ["meta", "data", "tail", "x"])
        _check_consistent(container, [0, 3, "3", 9, 10])
        records = doc.records_node()
        assert records.find_child_index(4) == 4


def test_wrapped_object_container(tmp_path: Path) -> None:
    data = b'{"meta": 1, "items": {"a": {"n": 1}, "b": {"n": 2}, "a": {"n": 3}}}'
    with _open(tmp_path / "wo.json", data, record_path="items") as doc:
        assert doc.kind == "wrapped" and doc.member_records
        records = doc.records_node()
        assert records.find_child_index("a") == 2
        assert records.find_child_index("b") == 1
        assert records.find_child_index("c") is None
        _check_consistent(records, ["a", "b", "c"])


def test_on_disk_children(tmp_path: Path) -> None:
    """More children than large_node_children: the child list lives on disk."""
    n = 300
    inner = {f"k{i}": i for i in range(n)}
    body = json.dumps(inner)[:-1] + ', "k5": "last"}'   # a duplicate key after the 300 members
    arr = json.dumps(list(range(n)))
    data = ('{"obj": ' + body + ', "arr": ' + arr + "}").encode()
    settings = Settings(large_node_children=50)
    with _open(tmp_path / "big.json", data, format="single", settings=settings) as doc:
        obj = doc.node(("obj",))
        assert obj.find_child_index("k5") == n        # the appended duplicate member
        assert obj.child(n).value() == "last"
        assert obj.find_child_index("k299") == 299
        assert obj.find_child_index("k300") is None
        big = doc.node(("arr",))
        assert big.find_child_index(250) == 250
        assert big.find_child_index("299") == 299
        assert big.find_child_index(300) is None
        _check_consistent(obj, ["k0", "k5", "k299", "zz"])
        _check_consistent(big, [0, 250, 299, 300])


def test_find_child_index_cancel(tmp_path: Path) -> None:
    from jsonreshape.errors import Cancelled
    from jsonreshape.tasks import CancelToken

    data = ("{" + ",".join(f'"k{i}": {i}' for i in range(5000)) + "}").encode()
    with _open(tmp_path / "c.json", data, format="single") as doc:
        root = doc.root()
        token = CancelToken()
        token.cancel()
        with pytest.raises(Cancelled):
            root.find_child_index("k1", cancel=token)
        assert root.find_child_index("k4999") == 4999
