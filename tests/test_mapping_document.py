"""The default tree of a document (jsonreshape.mapping.infer_document and its helpers; docs/ARCHITECTURE.md §13.0
"Tree source") -- the one inference behind the CLI's ``flatten`` and the GUI's structure builder.

* every document kind (array, jsonl, stream, brace_list, wrapped incl. a record path through an array index,
  keyed, single) gives ``infer_tree`` of the same values (ids ignored; types, counts and samples included), both
  core backends, exact numbers, Unicode and escapes, UTF-16 with a BOM and gzip input;
* the sample = ``schema.sample_positions`` (``sample_source``: ``None`` for a full scan or a sample that covers
  every record), huge records left out and counted, the progress total = the bytes of the sampled records,
  cancellation;
* refusals: a single value over ``large_node_bytes``, a wrapped document whose records are object members, a
  record array that never closes;
* edited documents are inferred with their edits (overlay state);
* ``document_values`` (a designer's population) gives the values in source order (keyed: the member values).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import jsongen
from jsonreshape import codec, reader, schema
from jsonreshape import mapping as mp
from jsonreshape.errors import Cancelled
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress

RAW_RECORDS = [
    '{"id": 0, "n": 1.10, "big": 12345678901234567890, "e": 1e400, "z": -0.0, "s": "Oʻzbekiston \'Ўзбекистон\'",'
    ' "tags": ["a", 1], "o": {"city": "Toshkent", "l": "a\\u2028b"}}',
    '{"id": 1, "n": 0.1, "tags": [], "o": {"city": "Тошкент", "zip": null}, "kids": [{"id": 11, "kids": []}]}',
    '{"id": 2, "s": "x\\"y\\\\z", "o": "raw", "kids": [{"id": 21, "kids": [{"id": 211}]}]}',
    '{"id": 3, "n": true, "extra": {"deep": [[1, 2], [3]]}}',
    '{"id": 4}',
]
RECORDS = [codec.loads(r.encode("utf-8")) for r in RAW_RECORDS]


@pytest.fixture
def backend(core_backend):
    """Documents are indexed with this core backend."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    yield name
    reader.set_scanner_backend(old)


@pytest.fixture
def opened(tmp_path: Path):
    docs: list[Document] = []

    def make(data: bytes, name: str = "in.json", **kw: Any) -> Document:
        path = tmp_path / name
        path.write_bytes(data)
        doc = Document.open(path, use_cache=False, **kw)
        doc.build_index()
        docs.append(doc)
        return doc

    yield make
    for d in docs:
        d.close()


def outline(tree: mp.MappingTree) -> list[tuple]:
    """Everything of a tree but the ids."""
    return [(d, n.key, n.kind, n.src, n.is_elem, n.is_root, n.types, n.count, n.pcount, n.samples, n.open)
            for n, _p, d in tree.walk()]


def raw_document(kind: str) -> tuple[bytes, dict]:
    recs = [r.encode("utf-8") for r in RAW_RECORDS]
    if kind == "array":
        return b"[" + b",\n".join(recs) + b"]", {}
    if kind == "jsonl":
        return b"".join(r + b"\n" for r in recs), {}
    if kind == "stream":
        return b"\n".join(recs) + b"\n", {"format": "stream"}
    if kind == "brace_list":
        return b"{" + b",\n".join(recs) + b"}", {}
    if kind == "wrapped":
        return b'{"meta": {"v": 1.10, "list": [1, 2]}, "data": [' + b",".join(recs) + b'], "tail": "end"}', {}
    if kind == "keyed":
        return b"{" + b",".join(b'"k%d": ' % i + r for i, r in enumerate(recs)) + b"}", {}
    if kind == "single":
        return recs[0], {}
    raise AssertionError(kind)


# ----------------------------------------------------------------------------------------
# every document kind
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["array", "jsonl", "stream", "brace_list", "wrapped", "keyed", "single"])
def test_every_kind_gives_the_tree_of_its_values(backend, opened, kind: str) -> None:
    data, kw = raw_document(kind)
    doc = opened(data, "in.jsonl" if kind == "jsonl" else "in.json", **kw)
    assert doc.kind == kind
    res = mp.infer_document(doc.pinned(), sample=None)
    values = RECORDS[:1] if kind == "single" else RECORDS
    if kind == "wrapped":
        wrapper = codec.loads(b'{"meta": {"v": 1.10, "list": [1, 2]}, "data": [], "tail": "end"}')
        want = mp.infer_tree(values, root_kind="wrapped", wrapper=wrapper, record_path=("data",))
        assert res.wrapper == wrapper and res.record_path == ("data",)
        assert codec.dumps(res.wrapper) == b'{"meta":{"v":1.10,"list":[1,2]},"data":[],"tail":"end"}'
        assert res.tree.record_src == ("data",)
    else:
        want = mp.infer_tree(values, root_kind=mp.root_kind_for(kind))
        assert res.wrapper is None and res.record_path is None
    assert outline(res.tree) == outline(want)
    assert res.tree.root_kind == mp.root_kind_for(kind)
    assert res.full and res.source is None and res.skipped == 0
    assert res.values == len(values) and res.total == doc.record_count
    # exact numbers and Unicode reach the samples unchanged
    if kind != "single":
        prefix = {"wrapped": "data[].", "keyed": ""}.get(kind, "[].")
        assert res.tree.find(prefix + "n").samples[0] == "1.10"
        assert res.tree.find(prefix + "big").samples == ("12345678901234567890",)
        assert res.tree.find(prefix + "e").samples == ("1e400",)


@pytest.mark.parametrize("encoding", ["utf-16-le", "gzip"])
def test_encodings_and_compression(opened, tmp_path: Path, encoding: str) -> None:
    recs = [{"id": i, "city": ["Toshkent", "Тошкент", "Oʻzbekiston"][i % 3]} for i in range(7)]
    path = tmp_path / ("e.json.gz" if encoding == "gzip" else "e.json")
    if encoding == "gzip":
        jsongen.write_file(path, recs, "array", gz=True)
    else:
        jsongen.write_file(path, recs, "array", encoding=encoding, bom=True)
    doc = Document.open(path, use_cache=False)
    doc.build_index()
    try:
        res = mp.infer_document(doc.pinned(), sample=3)
        positions = schema.sample_positions(7, 3)
        assert res.source == positions and not res.full and res.values == 3
        assert outline(res.tree) == outline(mp.infer_tree([recs[i] for i in positions]))
    finally:
        doc.close()


def test_wrapped_record_path_through_an_array_index(opened) -> None:
    data = (b'{"a": [{"items": [{"x": 1, "t": [1]}, {"x": 2}], "k": "first"}, {"items": [{"x": 3}], "k": "second"}],'
            b' "n": 1}')
    doc = opened(data, record_path=["a", 0, "items"])
    assert doc.kind == "wrapped" and tuple(doc.record_path) == ("a", 0, "items")
    res = mp.infer_document(doc.pinned())
    wrapper = codec.loads(data)
    wrapper["a"][0]["items"] = []
    assert res.wrapper == wrapper and res.record_path == ("a", 0, "items")
    want = mp.infer_tree([{"x": 1, "t": [1]}, {"x": 2}], root_kind="wrapped", wrapper=wrapper,
                         record_path=("a", 0, "items"))
    assert outline(res.tree) == outline(want)
    assert res.tree.record_src == ("a", mp.WILD, "items")


# ----------------------------------------------------------------------------------------
# sample, huge records, progress, cancellation
# ----------------------------------------------------------------------------------------
def test_sample_source() -> None:
    assert mp.sample_source(50, 10) == schema.sample_positions(50, 10)
    assert mp.sample_source(50, None) is None
    assert mp.sample_source(10, 10) is None and mp.sample_source(9, 10) is None     # covers every record
    assert mp.sample_source(0, 10) is None
    assert mp.sample_source(5, 1) == [0] and mp.sample_source(5, 0) == [0]
    assert mp.sample_source(10**12, 3) == [0, (10**12 - 1) // 2, 10**12 - 1]


def test_the_sample_positions_and_a_full_scan(opened) -> None:
    recs = [{"id": i, ("rare" if i == 1 else "common"): i} for i in range(50)]
    doc = opened(jsongen.build_document(recs, "array"))
    pinned = doc.pinned()
    positions = schema.sample_positions(50, 10)
    assert 1 not in positions
    res = mp.infer_document(pinned, sample=10)
    assert res.source == positions and not res.full and res.values == 10 and res.total == 50
    assert res.tree.find("[].rare") is None and res.tree.find("[]").count == 10
    assert outline(res.tree) == outline(mp.infer_tree([recs[i] for i in positions]))
    full = mp.infer_document(pinned, sample=None)
    assert full.full and full.values == 50 and full.tree.find("[].rare") is not None
    assert mp.infer_document(pinned, sample=50).full                      # the sample covers every record
    # a designer's population: the same records in the same order
    assert list(mp.document_values(pinned, res.source)) == [recs[i] for i in positions]
    assert list(mp.document_values(pinned, [7, 3, 7])) == [recs[7], recs[3], recs[7]]


def test_huge_records_are_left_out_and_counted(opened) -> None:
    recs = [{"id": i} for i in range(30)]
    recs[10] = {"id": 10, "blob": "h" * 6000, "only_in_huge": True}
    recs[20] = None                                                      # a null record is a value, not huge
    doc = opened(jsongen.build_document(recs, "jsonl"), "h.jsonl", settings=Settings(large_node_bytes=4096))
    pinned = doc.pinned()
    assert pinned.is_huge(10) and not pinned.is_huge(20)
    full = mp.infer_document(pinned)
    assert full.values == 29 and full.skipped == 1 and full.tree.find("[].only_in_huge") is None
    assert full.tree.find("[]").types == ("object", "null")
    counts = [0, 0]
    got = list(mp.document_values(pinned, None, counts=counts))
    assert counts == [29, 1] and got == [r for i, r in enumerate(recs) if i != 10]
    for n in (11, 9):                                                    # a sample without / with the huge record
        sample = mp.infer_document(pinned, sample=n)
        positions = schema.sample_positions(30, n)
        assert sample.source == positions
        assert sample.skipped == (1 if 10 in positions else 0) and sample.values == len(positions) - sample.skipped
    assert 10 in schema.sample_positions(30, 9) and sample.skipped == 1


def test_progress_and_cancellation(opened) -> None:
    recs = [{"id": i, "s": "x" * (i % 7)} for i in range(200)]
    doc = opened(jsongen.build_document(recs, "array"))
    pinned = doc.pinned()
    progress = Progress()
    res = mp.infer_document(pinned, sample=20, progress=progress)
    want = sum(e - s for s, e in map(pinned.record_span, res.source))
    assert progress.total == want and 0 < progress.done <= want
    progress = Progress()
    mp.infer_document(pinned, sample=None, progress=progress)
    assert progress.total and progress.done == progress.total
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        mp.infer_document(pinned, sample=None, cancel=token)


# ----------------------------------------------------------------------------------------
# refusals
# ----------------------------------------------------------------------------------------
def test_refusals(opened) -> None:
    big = opened(json.dumps({"blob": "b" * 5000}).encode(), "big.json", settings=Settings(large_node_bytes=4096))
    assert big.kind == "single"
    with pytest.raises(mp.MappingError) as info:
        mp.infer_document(big.pinned())
    assert info.value.key == "engine.error.too_large"
    members = opened(b'{"meta": 1, "users": {"a": {"x": 1}, "b": {"x": 2}}}', "m.json", record_path="users")
    assert members.kind == "wrapped" and members.member_records
    for fn in (lambda: mp.infer_document(members.pinned()), lambda: mp.document_wrapper(members.pinned())):
        with pytest.raises(mp.MappingError) as info:
            fn()
        assert info.value.key == "engine.error.member_container"
    partial = opened(b'{"meta": {"v": 1}, "data": [{"a": 1}, {"a": 2}, {"a": ', "p.json")
    if partial.kind != "wrapped":
        pytest.skip(f"the reader opened the partial wrapper as {partial.kind}")
    assert partial.error is not None
    with pytest.raises(mp.MappingError) as info:
        mp.infer_document(partial.pinned())
    assert info.value.key == "engine.error.partial_wrapper"


# ----------------------------------------------------------------------------------------
# edited documents
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["array", "keyed"])
def test_edited_documents_are_inferred_with_their_edits(opened, tmp_path: Path, fmt: str) -> None:
    from overlay_helpers import OverlayBuilder

    recs = [{"id": i, "name": f"n{i}"} for i in range(12)]
    doc = opened(jsongen.build_document(recs, fmt), f"e_{fmt}.json")
    member = doc.member_records

    def head(k: str) -> bytes:
        return b'"%s": ' % k.encode() if member else b""

    ob = OverlayBuilder(doc, tmp_path / f"layer_{fmt}")
    ob.set(3, head("zz") + '{"id": 300, "added": 1.10, "name": "Oʻzgardi"}'.encode())
    ob.insert(0, head("new") + b'{"id": -1, "fresh": {"x": [1]}}').delete(7)
    doc.set_overlay(ob.state())
    pinned = doc.pinned()
    res = mp.infer_document(pinned)
    values = []
    for raw in ob.records:
        if member:
            raw = raw[reader.split_member(raw)[1]:]
        values.append(codec.loads(raw))
    assert res.values == len(values) == pinned.record_count == 12
    want = mp.infer_tree(values, root_kind="keyed" if member else "records")
    assert outline(res.tree) == outline(want)
    prefix = "" if member else "[]."
    assert res.tree.find(prefix + "added").samples == ("1.10",) and res.tree.find(prefix + "fresh.x[]") is not None
