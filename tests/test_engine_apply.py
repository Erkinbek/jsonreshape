"""Streaming application of a mapping to whole documents (jsonreshape.engine.apply, docs/ARCHITECTURE.md §13.3).

The oracle is the in-memory transform of the whole parsed document (``CompiledMapping.transform_value``, itself
checked against the reference in test_engine.py) written with ``codec.dumps(value, indent)``: ``apply`` must give
exactly those bytes for every document kind (array, jsonl, stream, brace_list, wrapped incl. wrapper-level gathers
and broadcasts, keyed, single), every layout (indent 2/4/tab/compact, JSON Lines), both core backends, exact
numbers and Unicode; plus multi-pass root merges, the "no array reached" fallback, huge records (error / skip),
edited documents, selections, cancellation, progress, the unknown-fields count, errors and worker processes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

import jsongen
from jsonreshape import codec, engine, reader
from jsonreshape.engine import ApplyOptions, CompiledMapping, apply
from jsonreshape.errors import Cancelled, UserInputError
from jsonreshape.mapping import Designer, MapNode, MappingError, MappingTree, WILD, infer_tree
from jsonreshape.reader import Document
from jsonreshape.settings import Settings
from jsonreshape.tasks import CancelToken, Progress

RECORD_KINDS = ("array", "jsonl", "stream", "brace_list")
LAYOUTS = [ApplyOptions(), ApplyOptions(indent=None), ApplyOptions(indent=4), ApplyOptions(indent="tab"),
           ApplyOptions(jsonl=True)]
LAYOUT_IDS = ["indent2", "compact", "indent4", "tab", "jsonl"]


# ----------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------
@pytest.fixture
def backend(core_backend, monkeypatch):
    """Documents are indexed with this core backend and the engine formats with its Reformatter."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    monkeypatch.setattr(engine, "core", core_backend)
    yield name
    reader.set_scanner_backend(old)


@pytest.fixture
def make_doc(tmp_path: Path):
    docs: list[Document] = []
    src = tmp_path / "src"
    src.mkdir()

    def make(recs: Any = None, fmt: str = "array", *, data: bytes | None = None, name: str | None = None,
             settings: Settings | None = None, record_path: Any = None, format: str | None = None,
             **kw: Any) -> Document:
        p = src / (name or f"in{len(docs)}.{'jsonl' if fmt == 'jsonl' else 'json'}")
        if data is None:
            jsongen.write_file(p, recs, fmt, **kw)
        else:
            p.write_bytes(data)
        doc = Document.open(p, settings=settings, record_path=record_path, format=format)
        doc.build_index()
        docs.append(doc)
        return doc

    yield make
    for d in docs:
        d.close()


def values_of(doc: Document) -> list:
    return [doc.record(i) for i in range(doc.record_count)]


def expected(value: Any, opts: ApplyOptions) -> bytes:
    """The bytes apply must write for the mapped document value."""
    if opts.jsonl:
        items = value if isinstance(value, list) else [value]
        return b"".join(codec.dumps(x) + b"\n" for x in items)
    return codec.dumps(value, indent=opts.indent)


def run(doc: Document, tree: Any, tmp_path: Path, opts: ApplyOptions | None = None, **kw: Any):
    out = tmp_path / "out" / "result.json"
    out.parent.mkdir(exist_ok=True)
    rep = apply(doc, tree, out, opts or ApplyOptions(), **kw)
    return out.read_bytes(), rep


def restructured(recs: list) -> MappingTree:
    """A mapping with a move, a gather, a broadcast, a group and a recursive flatten (when possible)."""
    d = Designer(infer_tree(recs, root_kind="records"), population=lambda: iter(recs), undo_limit=500)
    t = d.tree
    for ref, target, zone in (("[].contact.address.city", "[].name", "before"),
                              ("[].orders[].oid", "[].name", "after"),
                              ("[].name", "[].orders[]", "inside")):
        n, tg = t.find(ref), t.find(target)
        if n is not None and tg is not None:
            d.move(n, tg, zone)
    if t.find("[]") is not None and t.find("[]").kind == "object":
        d.add_group(t.find("[]"), "holat")
        if t.find("[].note") is not None and t.find("[].holat") is not None:
            d.move(t.find("[].note"), t.find("[].holat"), "inside")
    ch = d.tree.find("[].extra")
    if ch is not None and d.can_flatten(ch):
        d.flatten(ch)
    return d.tree


CHILDREN = [
    {"id": 1, "name": "p1", "children": [{"name": "child1", "children": [{"name": "child1.2", "children": [
        {"name": "child1.2.1", "children": []}, {"name": "child1.2.3", "children": []}]}]},
        {"name": "child2", "children": []}]},
    {"id": 2, "name": "p2", "children": []},
]


# ----------------------------------------------------------------------------------
# record documents
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", RECORD_KINDS)
@pytest.mark.parametrize("li", range(len(LAYOUTS)), ids=LAYOUT_IDS)
def test_record_kinds_equal_the_whole_transform(make_doc, tmp_path: Path, backend: str, fmt: str, li: int) -> None:
    opts = LAYOUTS[li]
    recs = jsongen.records(40, seed=7)
    doc = make_doc(recs, fmt, style="pretty2" if fmt != "jsonl" else "compact")
    vals = values_of(doc)
    tree = restructured(vals)
    data, rep = run(doc, tree, tmp_path, opts)
    want = CompiledMapping(tree).transform_value(vals)
    assert data == expected(want, opts)
    assert rep.records_in == 40 and rep.items_out == 40 and rep.passes == 1
    assert rep.files == [tmp_path / "out" / "result.json"] and rep.bytes_written == len(data)
    assert rep.unmapped_records == 0 and rep.skipped_huge == 0 and rep.workers == 1


@pytest.mark.parametrize("style", ["compact", "messy", "escaped", "tab"])
def test_identity_keeps_exact_tokens_unicode_and_escapes(make_doc, tmp_path: Path, style: str) -> None:
    text = ('[{"n":[0.1,1e400,12345678901234567890,-0.0,1.10,-0,1E5,5e-324],"ism":"Oʻzbekiston Тошкент 😀",'
            '"q":"a\\"b\\\\c\\u0001\\u2028","k":{"10":1,"2":2}},{"n":[],"ism":"G\'ulom","k":{}}]')
    recs = codec.loads(text)
    doc = make_doc(recs, "array", style=style)
    tree = infer_tree(values_of(doc), root_kind="records")
    for opts in (ApplyOptions(indent=None), ApplyOptions(jsonl=True)):
        data, _ = run(doc, tree, tmp_path, opts)
        assert data == expected(recs, opts)
    data, _ = run(doc, tree, tmp_path, ApplyOptions(indent=None))
    assert b"1e400" in data and b"12345678901234567890" in data and b"-0.0" in data and b"1.10" in data
    assert "Oʻzbekiston Тошкент 😀".encode() in data and b"\\u0001" in data


def test_flatten_children_test2_streamed(make_doc, tmp_path: Path) -> None:
    doc = make_doc(CHILDREN, "jsonl")
    d = Designer(infer_tree(values_of(doc), root_kind="records"), population=lambda: iter(values_of(doc)))
    assert d.flatten(d.tree.find("[].children")).applied
    data, rep = run(doc, d.tree, tmp_path, ApplyOptions(indent=None))
    out = codec.loads(data)
    assert [k["name"] for k in out[0]["children"]] == ["child1", "child1.2", "child1.2.1", "child1.2.3", "child2"]
    assert [k["_depth"] for k in out[0]["children"]] == [1, 2, 3, 3, 1]
    assert out[1] == {"id": 2, "name": "p2", "children": []}


@pytest.mark.parametrize("indent", [0, 1, 3, "\t"])
def test_other_indents(make_doc, tmp_path: Path, indent: Any) -> None:
    recs = jsongen.records(12, seed=4)
    doc = make_doc(recs, "brace_list")
    tree = restructured(values_of(doc))
    data, _ = run(doc, tree, tmp_path, ApplyOptions(indent=indent))
    assert data == codec.dumps(CompiledMapping(tree).transform_value(values_of(doc)), indent=indent)


def test_root_flatten_streams_records_and_descendants(make_doc, tmp_path: Path) -> None:
    doc = make_doc(CHILDREN, "stream")
    vals = values_of(doc)
    d = Designer(infer_tree(vals, root_kind="records"), population=lambda: iter(vals))
    assert d.flatten(d.tree.find("")).applied
    want = CompiledMapping(d.tree).transform_value(vals)
    assert [x["name"] for x in want] == ["p1", "child1", "child1.2", "child1.2.1", "child1.2.3", "child2", "p2"]
    for opts in LAYOUTS:
        data, rep = run(doc, d.tree, tmp_path, opts)
        assert data == expected(want, opts) and rep.items_out == 7 and rep.records_in == 2


@pytest.mark.parametrize("li", range(len(LAYOUTS)), ids=LAYOUT_IDS)
def test_root_merge_needs_one_pass_per_element_node(make_doc, tmp_path: Path, li: int) -> None:
    opts = LAYOUTS[li]
    recs = [{"n": i, "k": [{"m": f"a{i}"}, {"m": f"b{i}"}] if i % 3 else "skip"} for i in range(9)]
    doc = make_doc(recs, "array")
    vals = values_of(doc)
    d = Designer(infer_tree(vals, root_kind="records"))
    assert d.move(d.tree.find("[].k"), d.tree.find(""), "inside").applied
    data, rep = run(doc, d.tree, tmp_path, opts)
    want = CompiledMapping(d.tree).transform_value(vals)
    assert want[:9] == [{"n": i} for i in range(9)] and len(want) == 9 + 12
    assert data == expected(want, opts)
    assert rep.passes == 2 and "engine.warn.passes" in rep.warning_keys and rep.items_out == 21


@pytest.mark.parametrize("recs, want", [
    ([{"k": "x"}, {"k": [1]}, {"k": "y"}], [1]),
    ([{"k": "x"}, {"k": "y"}], "x"),
    ([{"a": 1}], None),
    ([], None),
])
def test_no_array_reached_gives_the_reference_fallback(make_doc, tmp_path: Path, recs: list, want: Any) -> None:
    doc = make_doc(recs, "jsonl", name="f.jsonl") if recs else make_doc(data=b"[]")
    d = Designer(infer_tree([{"r": 1, "k": [1]}], root_kind="records"))
    assert d.move(d.tree.find("[].k"), d.tree.find(""), "inside").applied
    assert d.delete(d.tree.find("[]")).applied
    assert CompiledMapping(d.tree).transform_value(values_of(doc)) == want
    data, rep = run(doc, d.tree, tmp_path, ApplyOptions(indent=None))
    assert codec.loads(data) == want
    assert ("engine.warn.not_array" in rep.warning_keys) == (not isinstance(want, list))


def test_empty_record_documents(make_doc, tmp_path: Path) -> None:
    doc = make_doc(data=b"[ ]")
    tree = infer_tree([{"a": 1}], root_kind="records")
    for opts, want in ((ApplyOptions(), b"[]"), (ApplyOptions(indent=None), b"[]"), (ApplyOptions(jsonl=True), b"")):
        data, rep = run(doc, tree, tmp_path, opts)
        assert data == want and rep.items_out == 0 and rep.records_in == 0


@pytest.mark.parametrize("data, name", [
    (b"[null, null]", None),
    (b"[1, null]", None),
    (b"null\nnull\n", "scalars.jsonl"),
    (b'[1, "a", true]', None),
    (b"[[1, 2], [3]]", None),
    (b'[1, "a", [2], {"b": 1}, null]', None),
])
@pytest.mark.parametrize("jsonl", [False, True])
def test_scalar_and_mixed_records_with_the_default_tree(make_doc, tmp_path: Path, backend: str, data: bytes,
                                                         name: str | None, jsonl: bool) -> None:
    # regression: a record element whose evaluation is the identity (scalar records) was appended with
    # ``list.append(record, out)`` -- a TypeError for scalars, and a list record got ``out`` appended to it
    doc = make_doc(data=data, name=name, format="jsonl" if name else None)
    vals = values_of(doc)
    tree = infer_tree(vals, root_kind="records")
    opts = ApplyOptions(jsonl=jsonl)
    out, rep = run(doc, tree, tmp_path, opts)
    want = CompiledMapping(tree).transform_value(vals)
    assert out == expected(want, opts)
    assert rep.records_in == len(vals) and vals == values_of(doc)


def test_selection_in_its_order(make_doc, tmp_path: Path) -> None:
    recs = [{"i": i, "v": [i, i + 1]} for i in range(30)]
    doc = make_doc(recs, "array")
    tree = infer_tree(recs, root_kind="records")
    for source in ([5, 1, 29, 1], range(10, 20)):
        data, rep = run(doc, tree, tmp_path, ApplyOptions(indent=None), source=source)
        assert codec.loads(data) == [recs[i] for i in source]
        assert rep.records_in == len(source)
    with pytest.raises(UserInputError) as ei:
        run(doc, tree, tmp_path, source=[30])
    assert ei.value.key == "writer.error.bad_record"


def test_inputs_with_bom_crlf_gzip_and_utf16(make_doc, tmp_path: Path) -> None:
    recs = jsongen.records(15, seed=3)
    tree = infer_tree(recs, root_kind="records")
    want = CompiledMapping(tree).transform_value(recs)
    for kw in ({"bom": True}, {"gz": True}, {"encoding": "utf-16-le", "bom": True}, {"newline": "\r\n"}):
        fmt = "jsonl" if "newline" in kw else "array"
        doc = make_doc(recs, fmt, style="pretty2", **kw)
        data, _ = run(doc, tree, tmp_path)
        assert data == codec.dumps(want, indent=2), kw


# ----------------------------------------------------------------------------------
# wrapped documents
# ----------------------------------------------------------------------------------
def wrapped_case(make_doc, recs: list, path: tuple = ("data",), **kw: Any) -> tuple[Document, Any, Designer]:
    doc = make_doc(recs, "wrapped", wrapper_path=path, style="pretty2", record_path=path, **kw)
    assert doc.kind == "wrapped" and tuple(doc.record_path) == path
    whole = codec.loads(Path(doc.source_path).read_bytes())
    tree = infer_tree(values_of(doc), root_kind="wrapped", wrapper=whole, record_path=path)
    d = Designer(tree, population=lambda: iter(values_of(doc)), wrapper=whole, record_path=path, undo_limit=500)
    return doc, whole, d


@pytest.mark.parametrize("li", range(len(LAYOUTS) - 1), ids=LAYOUT_IDS[:-1])
@pytest.mark.parametrize("path", [("data",), ("result", "items")], ids=["data", "result.items"])
def test_wrapped_identity_gather_and_broadcast(make_doc, tmp_path: Path, backend: str, li: int,
                                               path: tuple) -> None:
    opts = LAYOUTS[li]
    recs = jsongen.records(25, seed=11)
    doc, whole, d = wrapped_case(make_doc, recs, path)
    data, rep = run(doc, d.tree, tmp_path, opts)
    assert data == expected(CompiledMapping(d.tree).transform_value(whole), opts)
    assert rep.passes == 1 and rep.items_out == 25
    t = d.tree
    rp = ".".join(path)
    assert d.move(t.find(f"{rp}[].orders[].oid"), t.find("meta"), "after").applied          # wrapper-level gather
    assert d.move(t.find("meta.version"), t.find(f"{rp}[]"), "inside").applied              # broadcast
    assert d.add_group(t.find(""), "jami").applied
    assert d.move(t.find("tail.count"), t.find("jami"), "inside").applied
    want = CompiledMapping(d.tree).transform_value(whole)
    data, rep = run(doc, d.tree, tmp_path, opts)
    assert data == expected(want, opts)
    assert rep.passes == 2 and "engine.warn.passes" in rep.warning_keys
    assert rep.items_out == 25 + sum(len(r.get("orders") or []) for r in recs if isinstance(r.get("orders"), list))


def test_wrapped_jsonl_writes_the_record_array(make_doc, tmp_path: Path) -> None:
    recs = jsongen.records(10, seed=5)
    doc, whole, d = wrapped_case(make_doc, recs)
    assert d.rename(d.tree.find("data"), "rows").applied
    data, rep = run(doc, d.tree, tmp_path, ApplyOptions(jsonl=True))
    want = CompiledMapping(d.tree).transform_value(whole)["rows"]
    assert data == b"".join(codec.dumps(x) + b"\n" for x in want)
    assert "engine.warn.jsonl_wrapper" in rep.warning_keys and rep.items_out == 10


def test_wrapped_raw_record_array_pass_through(make_doc, tmp_path: Path) -> None:
    recs = [{"a": 1, "b": [1, 2]}, {"a": "x"}]
    doc, whole, d = wrapped_case(make_doc, recs)
    # a value node reading the whole record array (a crafted tree): written from the raw record text
    root = MapNode(id=1, kind="object", src=(), is_root=True, children=[
        MapNode(id=2, key="copy", kind="value", src=("data",)),
        MapNode(id=3, key="meta", kind="value", src=("meta",))])
    tree = MappingTree(root, root_kind="wrapped", record_src=("data",))
    for opts in (ApplyOptions(), ApplyOptions(indent=None), ApplyOptions(indent="\t")):
        data, _ = run(doc, tree, tmp_path, opts)
        assert data == codec.dumps({"copy": whole["data"], "meta": whole["meta"]}, indent=opts.indent)


def test_wrapped_with_an_edited_wrapper_and_records(make_doc, tmp_path: Path) -> None:
    from overlay_helpers import OverlayBuilder, open_expected

    recs = [{"i": i, "s": f"r{i}"} for i in range(12)]
    doc, whole, d = wrapped_case(make_doc, recs)
    assert d.move(d.tree.find("meta.version"), d.tree.find("data[]"), "inside").applied
    ob = OverlayBuilder(doc, tmp_path / "layer")
    ob.set(2, b'{"i": 200, "s": "edited"}').insert(0, b'{"i": -1}').delete(5)
    ob.set_wrapper(b'{"meta": {"version": 7, "source": "edit", "list": []}, "data": [], "tail": {"ok": false}}')
    doc.set_overlay(ob.state())
    plain = open_expected(doc, ob.records, tmp_path / "expected.json", wrapper=ob.wrapper_text)
    try:
        got, _ = run(doc, d.tree, tmp_path)
        want, _ = run(plain, d.tree, tmp_path)
        assert got == want
        assert codec.loads(got)["data"][0] == {"i": -1, "version": 7}
    finally:
        plain.close()


def test_wrapped_record_path_through_an_array_index(make_doc, tmp_path: Path) -> None:
    data = (b'{"a": [{"items": [{"x": 1, "t": [1]}, {"x": 2}], "k": "first"}, {"items": [{"x": 3}], "k": "second"}],'
            b' "m": {"v": 1.10}}')
    doc = make_doc(data=data, record_path=("a", 0, "items"))
    assert doc.kind == "wrapped" and doc.record_count == 2
    whole = codec.loads(data)
    tree = infer_tree(values_of(doc), root_kind="wrapped", wrapper=whole, record_path=("a", 0, "items"))
    d = Designer(tree, population=lambda: iter(values_of(doc)), wrapper=whole, record_path=("a", 0, "items"))
    assert d.move(d.tree.find("m.v"), d.tree.find("a[].items[]"), "inside").applied            # broadcast
    assert d.move(d.tree.find("a[].items[].x"), d.tree.find("a[].k"), "after").applied         # gather per a[]
    want = CompiledMapping(d.tree).transform_value(whole)
    for opts in LAYOUTS[:-1]:
        out, rep = run(doc, d.tree, tmp_path, opts)
        assert out == expected(want, opts)


def test_nested_reads_of_the_record_array_are_exact(make_doc, tmp_path: Path) -> None:
    # crafted trees whose records read the whole record array: a second pass inside every record (exact,
    # never handed to worker processes)
    recs = [{"i": 0}, {"i": 1, "s": "Oʻ"}, {"i": 2}]
    doc = make_doc(recs, "array")
    elem = MapNode(id=2, kind="object", src=(WILD,), is_elem=True, children=[
        MapNode(id=3, key="i", src=(WILD, "i")), MapNode(id=4, key="all", src=())])
    tree = MappingTree(MapNode(id=1, kind="array", src=(), is_root=True, children=[elem]), root_kind="records")
    want = [{"i": r["i"], "all": recs} for r in recs]
    assert CompiledMapping(tree).transform_value(recs) == want
    for opts in (ApplyOptions(), ApplyOptions(indent=None), ApplyOptions(jsonl=True)):
        out, rep = run(doc, tree, tmp_path, replace_workers(opts, 2))
        assert out == expected(want, opts)
        assert rep.workers == 1 and rep.passes == 4


def test_worker_count_rule() -> None:
    default = Settings()
    assert engine.auto_workers(10 << 20, default) == 1                  # small inputs: one process
    by_memory = (default.memory_limit_bytes // 2) // engine.WORKER_MEMORY   # 4 with the default 512 MiB
    assert engine.auto_workers(1 << 30, default) == max(1, min((os.cpu_count() or 1) - 1, 8, by_memory))
    assert engine.auto_workers(1 << 30, Settings(memory_limit_bytes=100 << 20)) == 1


def test_transform_record_needs_a_root_array() -> None:
    cm = CompiledMapping(infer_tree([{"a": 1}], root_kind="value"))
    with pytest.raises(ValueError):
        cm.transform_record({"a": 1})


def test_wrapped_with_object_container_is_refused(make_doc, tmp_path: Path) -> None:
    data = b'{"meta": 1, "users": {"a": {"x": 1}, "b": {"x": 2}, "c": {"x": 3}}}'
    doc = make_doc(data=data, record_path="users")
    assert doc.kind == "wrapped" and doc.member_records
    tree = infer_tree(values_of(doc), root_kind="wrapped", wrapper=codec.loads(data), record_path=("users",))
    with pytest.raises(MappingError) as ei:
        run(doc, tree, tmp_path)
    assert ei.value.key == "engine.error.member_container"
    assert not (tmp_path / "out" / "result.json").exists()


# ----------------------------------------------------------------------------------
# keyed and single documents
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("li", range(len(LAYOUTS)), ids=LAYOUT_IDS)
def test_keyed_members_mapped_with_their_keys(make_doc, tmp_path: Path, backend: str, li: int) -> None:
    opts = LAYOUTS[li]
    members = [{"id": i, "name": f"n{i}", "contact": {"city": "Toshkent", "zip": i}} for i in range(6)]
    keys = ["a", "b", "a", "Тошкент", "x\"y", "e"]                      # a repeated key stays two members
    doc = make_doc(members, "keyed", keys=keys, style="pretty2")
    assert doc.kind == "keyed"
    d = Designer(infer_tree(values_of(doc), root_kind="keyed"), population=lambda: iter(values_of(doc)))
    assert d.move(d.tree.find("contact.city"), d.tree.find("id"), "before").applied
    data, rep = run(doc, d.tree, tmp_path, opts)
    cm = CompiledMapping(d.tree)
    pairs = [(codec.dumps(k), cm.transform_value(v)) for k, v in zip(keys, members)]
    if opts.jsonl:
        want = b"".join(b"{" + k + b":" + codec.dumps(v) + b"}\n" for k, v in pairs)
    else:                                   # one Reformatter over the compact text: the layout of every export
        want = _reformat(b"{" + b",".join(k + b":" + codec.dumps(v) for k, v in pairs) + b"}", opts.indent)
    assert data == want
    assert rep.items_out == 6 and rep.records_in == 6


def _reformat(compact: bytes, indent: Any) -> bytes:
    from jsonreshape import core

    rf = core.Reformatter(indent)
    return rf.feed(compact) + rf.finish()


def test_keyed_raw_key_tokens_and_missing_members(make_doc, tmp_path: Path) -> None:
    data = b'{"\\u0041": {"x": [1, 2]}, "b": {"y": 1}, "c": {"x": "s"}, "d": {"x": []}}'
    doc = make_doc(data=data)
    assert doc.kind == "keyed"
    # root = the member value as an array node whose only element node reads "x" below it
    elem = MapNode(id=2, kind="value", src=("x", WILD), is_elem=True)
    tree = MappingTree(MapNode(id=1, kind="array", src=(), is_root=True, children=[elem]), root_kind="keyed")
    out, rep = run(doc, tree, tmp_path, ApplyOptions(indent=None))
    assert out == b'{"\\u0041":[1,2],"c":"s","d":[]}'                   # "b": MISSING, not written
    assert rep.items_out == 3


def test_single_document(make_doc, tmp_path: Path) -> None:
    text = b'{"list": [{"a": 1}, {"a": 2}], "meta": {"v": 1.10}}'
    value = codec.loads(text)
    doc = make_doc(data=text, format="single")
    assert doc.kind == "single"
    d = Designer(infer_tree([value], root_kind="value"), population=lambda: iter([value]))
    assert d.move(d.tree.find("list[].a"), d.tree.find("list"), "before").applied
    assert d.move(d.tree.find("meta.v"), d.tree.find("list[]"), "inside").applied
    want = {"a": [1, 2], "list": [{"v": codec.loads("1.10")}, {"v": codec.loads("1.10")}], "meta": {}}
    for opts in LAYOUTS:
        data, rep = run(doc, d.tree, tmp_path, opts)
        assert data == (codec.dumps(want) + b"\n" if opts.jsonl else codec.dumps(want, indent=opts.indent))
        assert rep.items_out == 1


def test_single_document_too_large(make_doc, tmp_path: Path) -> None:
    value = {"big": "x" * 500}
    doc = make_doc(data=codec.dumps(value), format="single", settings=Settings(large_node_bytes=100))
    tree = infer_tree([value], root_kind="value")
    with pytest.raises(MappingError) as ei:
        run(doc, tree, tmp_path)
    assert ei.value.key == "engine.error.too_large" and ei.value.hint


# ----------------------------------------------------------------------------------
# huge records, edited documents, partial documents
# ----------------------------------------------------------------------------------
def test_huge_records_stop_or_are_skipped(make_doc, tmp_path: Path) -> None:
    recs = [{"i": 0}, {"i": 1, "pad": "x" * 400}, {"i": 2}, {"i": 3, "pad": "y" * 400}]
    doc = make_doc(recs, "array", settings=Settings(large_node_bytes=200))
    tree = infer_tree([recs[0], recs[1]], root_kind="records")
    target = tmp_path / "out" / "result.json"
    target.parent.mkdir()
    target.write_bytes(b"old")
    with pytest.raises(MappingError) as ei:
        apply(doc, tree, target, ApplyOptions())
    assert ei.value.key == "engine.error.huge_record" and ei.value.params["record"] == 1
    assert target.read_bytes() == b"old"                                  # atomic: nothing replaced
    data, rep = run(doc, tree, tmp_path, ApplyOptions(indent=None, skip_huge=True))
    assert codec.loads(data) == [{"i": 0}, {"i": 2}]
    assert rep.skipped_huge == 2 and "engine.warn.skipped_huge" in rep.warning_keys
    assert any("2" in w for w in rep.warnings)


@pytest.mark.parametrize("fmt", ["array", "jsonl", "keyed"])
def test_edited_documents_are_mapped_with_their_edits(make_doc, tmp_path: Path, fmt: str) -> None:
    from overlay_helpers import OverlayBuilder, open_expected

    recs = [{"id": i, "orders": [{"oid": i * 10}], "name": f"n{i}"} for i in range(20)]
    doc = make_doc(recs, fmt)
    member = doc.member_records
    vals = values_of(doc)
    root_kind = "keyed" if member else "records"
    d = Designer(infer_tree(vals, root_kind=root_kind))
    oid = "orders[].oid" if member else "[].orders[].oid"
    name = "name" if member else "[].name"
    assert d.move(d.tree.find(oid), d.tree.find(name), "after").applied
    ob = OverlayBuilder(doc, tmp_path / "layer")

    def head(k: str) -> bytes:
        return b'"%s": ' % k.encode() if member else b""
    body = '{"id": 300, "name": "Oʻzgardi", "orders": [{"oid": 1.10}, {"oid": 1e400}]}'.encode()
    ob.set(3, head("zz") + body).insert(0, head("new") + b'{"id": -1}').delete(7, 8).move([4], 0)
    doc.set_overlay(ob.state())
    plain = open_expected(doc, ob.records, tmp_path / ("expected.jsonl" if fmt == "jsonl" else "expected.json"))
    try:
        for opts in (ApplyOptions(), ApplyOptions(jsonl=True)):
            got, rep = run(doc, d.tree, tmp_path, opts)
            want, _ = run(plain, d.tree, tmp_path, opts)
            assert got == want
            assert rep.records_in == doc.record_count == 19
        assert b"1e400" in got and "Oʻzgardi".encode() in got
    finally:
        plain.close()


def test_partial_and_broken_documents_warn(make_doc, tmp_path: Path) -> None:
    doc = make_doc(data=b'[{"a": 1}, {"a": 2}, {"a": ')
    assert doc.error is not None
    tree = infer_tree([{"a": 1}], root_kind="records")
    data, rep = run(doc, tree, tmp_path, ApplyOptions(indent=None))
    assert codec.loads(data) == [{"a": 1}, {"a": 2}]
    assert "engine.warn.partial" in rep.warning_keys
    doc2 = make_doc(data=b'{"a": 1}\nnot json\n{"a": 3}\n', name="b.jsonl")
    data, rep = run(doc2, tree, tmp_path, ApplyOptions(jsonl=True))
    assert data == b'{"a":1}\n{"a":3}\n' and "engine.warn.broken_lines" in rep.warning_keys


# ----------------------------------------------------------------------------------
# the unknown-fields count (§13.0)
# ----------------------------------------------------------------------------------
def test_unmapped_records_are_counted(make_doc, tmp_path: Path) -> None:
    recs = [{"a": 1, "o": {"x": 1}}, {"a": 2, "o": {"x": 2, "new": 1}}, {"a": 3, "extra": True},
            {"a": 4, "o": "raw"}, {"a": 5, "drop": 1}]
    doc = make_doc(recs, "jsonl")
    sample = [{"a": 0, "o": {"x": 0}, "drop": 0}]
    d = Designer(infer_tree(sample, root_kind="records"))
    assert d.delete(d.tree.find("[].drop")).applied                 # deleted on purpose: known
    data, rep = run(doc, d.tree, tmp_path, ApplyOptions(indent=None))
    assert rep.unmapped_records == 2 and "engine.warn.unmapped" in rep.warning_keys
    assert codec.loads(data) == [{"a": 1, "o": {"x": 1}}, {"a": 2, "o": {"x": 2}}, {"a": 3}, {"a": 4, "o": "raw"},
                                 {"a": 5}]


def test_unmapped_wrapper_fields_warn(make_doc, tmp_path: Path) -> None:
    recs = [{"a": 1}]
    doc, whole, _ = wrapped_case(make_doc, recs)
    tree = infer_tree(recs, root_kind="wrapped", wrapper={"data": []}, record_path=("data",))
    data, rep = run(doc, tree, tmp_path, ApplyOptions(indent=None))
    assert codec.loads(data) == {"data": [{"a": 1}]}
    assert "engine.warn.unmapped_wrapper" in rep.warning_keys and rep.unmapped_records == 0


# ----------------------------------------------------------------------------------
# errors, options, progress, cancellation
# ----------------------------------------------------------------------------------
def test_target_checks_and_kind_mismatch(make_doc, tmp_path: Path) -> None:
    recs = [{"a": 1}]
    doc = make_doc(recs, "array")
    tree = infer_tree(recs, root_kind="records")
    for target, key in ((doc.source_path, "writer.error.same_file"), (tmp_path / "nodir" / "x.json", "writer.error.no_dir"),
                        (tmp_path, "fileio.error.is_directory")):
        with pytest.raises(UserInputError) as ei:
            apply(doc, tree, target)
        assert ei.value.key == key
    value_tree = infer_tree([{"a": 1}], root_kind="value")
    with pytest.raises(MappingError) as ei:
        run(doc, value_tree, tmp_path)
    assert ei.value.key == "engine.error.kind_mismatch" and "JSON massiv" in str(ei.value)
    keyed_tree = infer_tree([{"a": 1}], root_kind="keyed")
    with pytest.raises(MappingError):
        run(doc, keyed_tree, tmp_path)


def test_options_are_validated() -> None:
    assert ApplyOptions(indent="tab").indent == "\t"
    for bad in ({"indent": 17}, {"indent": "x"}, {"indent": True}, {"workers": 0}, {"workers": True}):
        with pytest.raises(UserInputError):
            ApplyOptions(**bad)
    with pytest.raises(TypeError):
        apply(None, None, "x", opts={"jsonl": True})    # type: ignore[arg-type]


def test_progress_is_bytes_and_reaches_the_total(make_doc, tmp_path: Path) -> None:
    recs = jsongen.records(200, seed=1)
    doc = make_doc(recs, "array", style="pretty2")
    infos = []
    prog = Progress(callback=infos.append, min_interval=0)
    _, rep = run(doc, infer_tree(values_of(doc), root_kind="records"), tmp_path, progress=prog)
    assert prog.finished and prog.done == prog.total == doc.data_size - doc.info.data_start
    assert prog.total - 16 <= rep.bytes_read <= prog.total          # the records' extent (brackets excluded)
    dones = [i.done for i in infos]
    assert dones == sorted(dones) and infos[-1].unit == "bytes"


def test_cancellation_leaves_no_output(make_doc, tmp_path: Path) -> None:
    recs = [{"i": i, "s": "x" * 200} for i in range(3000)]
    doc = make_doc(recs, "array")
    tree = infer_tree(recs[:5], root_kind="records")
    token = CancelToken()

    def cb(info: Any) -> None:
        if info.done > 0:
            token.cancel()
    prog = Progress(callback=cb, min_interval=0)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    with pytest.raises(Cancelled):
        apply(doc, tree, out_dir / "r.json", ApplyOptions(), progress=prog, cancel=token)
    assert list(out_dir.iterdir()) == []


def test_source_changed_before_commit(make_doc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jsonreshape.errors import FileChangedError

    recs = [{"i": 1}]
    doc = make_doc(recs, "array")
    tree = infer_tree(recs, root_kind="records")

    def changed(self: Any) -> None:
        raise FileChangedError(path=str(self.source_path))
    monkeypatch.setattr(Document, "ensure_unchanged", changed)
    with pytest.raises(FileChangedError):
        run(doc, tree, tmp_path)
    assert not (tmp_path / "out" / "result.json").exists()


def test_reports_and_messages_are_localized(make_doc, tmp_path: Path, lang_en: None) -> None:
    recs = [{"n": 1, "k": [{"m": 1}]}, {"n": 2, "k": [{"m": 2}]}]
    doc = make_doc(recs, "array")
    d = Designer(infer_tree(recs, root_kind="records"))
    d.move(d.tree.find("[].k"), d.tree.find(""), "inside")
    _, rep = run(doc, d.tree, tmp_path)
    assert rep.warnings == ["The records were read 2 times (separate passes for merged arrays)."]
    assert rep.to_dict()["passes"] == 2 and rep.mb_per_s >= 0


# ----------------------------------------------------------------------------------
# property: streaming == the whole transform, on random records and random edit scripts
# ----------------------------------------------------------------------------------
_KEYS = ["id", "name", "kids", "o", "a", "10", "Тошкент", ""]
_scalars = st.one_of(st.none(), st.booleans(), st.integers(-3, 3),
                     st.sampled_from(["0.1", "1.10", "-0.0", "1e400", "12345678901234567890"]).map(codec.loads),
                     st.sampled_from(["x", "Oʻzbek", "q\"t", "😀"]))
_values = st.recursive(_scalars, lambda inner: st.one_of(
    st.lists(inner, max_size=3), st.dictionaries(st.sampled_from(_KEYS), inner, max_size=3)), max_leaves=10)
_records = st.lists(st.dictionaries(st.sampled_from(_KEYS), _values, max_size=4), max_size=6)


def _random_ops(d: Designer, rng: Any, n: int) -> None:
    for _ in range(n):
        nodes = list(d.tree.iter_nodes())
        a, b = rng.choice(nodes), rng.choice(nodes)
        r = rng.random()
        try:
            if r < 0.5:
                d.move(a, b, rng.choice(("before", "inside", "after")))
            elif r < 0.6:
                d.delete(a)
            elif r < 0.7:
                d.unwrap(a)
            elif r < 0.8:
                d.add_group(a, "g")
            elif d.can_flatten(a):
                d.flatten(a)
        except ValueError:
            pass


@hsettings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(recs=_records, seed=st.integers(0, 10**6), nops=st.integers(0, 8), fmt=st.sampled_from(RECORD_KINDS),
       jsonl=st.booleans())
def test_streaming_equals_the_whole_transform_on_random_trees(recs: list, seed: int, nops: int, fmt: str,
                                                            jsonl: bool) -> None:
    import random
    import tempfile

    if fmt == "jsonl" and not recs:
        return                                           # an empty .jsonl file is no document
    rng = random.Random(seed)
    d = Designer(infer_tree(recs or [{"a": 1}], root_kind="records"), population=lambda: iter(recs), undo_limit=500)
    _random_ops(d, rng, nops)
    opts = ApplyOptions(indent=None, jsonl=jsonl)
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / ("in.jsonl" if fmt == "jsonl" else "in.json")
        jsongen.write_file(p, recs, fmt, style="messy", seed=seed)
        with Document.open(p, format=fmt) as doc:
            doc.build_index()
            out = Path(tmp) / "out.json"
            rep = apply(doc, d.tree, out, opts)
            want = CompiledMapping(d.tree).transform_value(codec.loads(codec.dumps(recs)))
            assert out.read_bytes() == expected(want, opts)
            assert rep.records_in == len(recs)


@hsettings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(recs=_records, meta=_values, seed=st.integers(0, 10**6), nops=st.integers(0, 8))
def test_wrapped_streaming_equals_the_whole_transform_on_random_trees(recs: list, meta: Any, seed: int,
                                                                    nops: int) -> None:
    import random
    import tempfile

    rng = random.Random(seed)
    whole = {"meta": meta, "data": recs, "tail": {"n": len(recs)}}
    text = codec.dumps(whole, indent=2)
    whole = codec.loads(text)
    tree = infer_tree(whole["data"], root_kind="wrapped", wrapper=whole, record_path=("data",))
    d = Designer(tree, population=lambda: iter(whole["data"]), wrapper=whole, record_path=("data",),
                 undo_limit=500)
    _random_ops(d, rng, nops)
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "in.json"
        p.write_bytes(text)
        with Document.open(p, record_path="data") as doc:
            doc.build_index()
            out = Path(tmp) / "out.json"
            apply(doc, d.tree, out, ApplyOptions(indent=None))
            assert out.read_bytes() == codec.dumps(CompiledMapping(d.tree).transform_value(whole))


# ----------------------------------------------------------------------------------
# worker processes (§13.2): identical bytes
# ----------------------------------------------------------------------------------
@pytest.mark.slow
def test_worker_processes_give_identical_bytes(make_doc, tmp_path: Path) -> None:
    recs = jsongen.records(400, seed=9)
    doc = make_doc(recs, "array", style="messy")
    vals = values_of(doc)
    tree = restructured(vals[:50])                       # a sample: some records have unknown fields
    small = infer_tree(vals[:3], root_kind="records")
    for tr in (tree, small):
        for opts in (ApplyOptions(), ApplyOptions(indent=None), ApplyOptions(jsonl=True), ApplyOptions(indent="\t")):
            one, rep1 = run(doc, tr, tmp_path, opts)
            many, rep2 = run(doc, tr, tmp_path, replace_workers(opts, 2))
            assert one == many
            assert rep1.unmapped_records == rep2.unmapped_records and rep2.workers == 2
            assert rep1.items_out == rep2.items_out
    # a root flatten and a root merge (two passes, both through the workers)
    d = Designer(infer_tree(CHILDREN * 20, root_kind="records"), population=lambda: iter(CHILDREN * 20))
    assert d.flatten(d.tree.find("")).applied
    merged = Designer(infer_tree(vals, root_kind="records"))
    assert merged.move(merged.tree.find("[].orders"), merged.tree.find(""), "inside").applied
    cdoc = make_doc(CHILDREN * 20, "jsonl")
    for tdoc, tr in ((cdoc, d.tree), (doc, merged.tree)):
        one, rep1 = run(tdoc, tr, tmp_path)
        many, rep2 = run(tdoc, tr, tmp_path, ApplyOptions(workers=2))
        assert one == many and rep1.passes == rep2.passes and rep2.workers == 2
    # wrapped: wrapper fields broadcast into the records (the workers get the wrapper)
    recs2 = [{"i": i} for i in range(50)]
    wdoc, whole, d = wrapped_case(make_doc, recs2)
    assert d.move(d.tree.find("meta.version"), d.tree.find("data[]"), "inside").applied
    one, _ = run(wdoc, d.tree, tmp_path)
    many, rep = run(wdoc, d.tree, tmp_path, ApplyOptions(workers=2))
    assert one == many == codec.dumps(CompiledMapping(d.tree).transform_value(whole), indent=2)
    assert rep.workers == 2


def replace_workers(opts: ApplyOptions, n: int) -> ApplyOptions:
    from dataclasses import replace

    return replace(opts, workers=n)


@pytest.mark.bench
def test_throughput_one_thread_and_workers(tmp_path: Path) -> None:
    """``pytest -m bench -s tests/test_engine_apply.py``: MB/s of an identity mapping and of a mapping with
    moves, a gather, a broadcast and a recursive flatten on ~200 MB of bench/make_synthetic.py records."""
    import sys
    import time

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
    from make_synthetic import generate  # type: ignore[import-not-found]

    from jsonreshape import schema

    src = tmp_path / "syn.json"
    generate(str(src), size=200 << 20, records=None, fmt="array", seed=1, quiet=True)
    doc = Document.open(src)
    doc.build_index()
    vals = [doc.record(i) for i in schema.sample_positions(doc.record_count, 10000)]
    ident = infer_tree(vals, root_kind="records")
    d = Designer(infer_tree(vals, root_kind="records"), population=lambda: iter(vals))
    t = d.tree
    d.move(t.find("[].contact.address.city"), t.find("[].name"), "before")
    d.move(t.find("[].orders[].oid"), t.find("[].name"), "after")
    d.move(t.find("[].name"), t.find("[].orders[]"), "inside")
    d.flatten(d.tree.find("[].children"))
    size = src.stat().st_size
    try:
        for label, tree in (("identity", ident), ("moves/gather/broadcast/flatten", d.tree)):
            for workers in (1, None):
                t0 = time.perf_counter()
                rep = apply(doc, tree, tmp_path / "out.json", ApplyOptions(workers=workers))
                dt = time.perf_counter() - t0
                print(f"\n{label}: {size / dt / 1e6:.1f} MB/s with {rep.workers} process(es)")
    finally:
        doc.close()


def test_spawn_safety_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    def main_module(name: str | None, file: str | None) -> types.ModuleType:
        m = types.ModuleType("__main__")
        m.__spec__ = types.SimpleNamespace(name=name) if name else None   # type: ignore[assignment]
        if file:
            m.__file__ = file
        return m
    for name, file, safe in (("jsonreshape.__main__", "x", True), ("pytest.__main__", "x", True), ("__main__", "z", True),
                             ("jsonreshape.gui.app", "x", True), ("mytool", "x", False), (None, "script.py", False),
                             (None, None, True)):
        monkeypatch.setitem(sys.modules, "__main__", main_module(name, file))
        assert engine._spawn_safe() is safe, (name, file)
