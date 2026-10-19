"""jsonreshape.mapping: node model, inference, designer operations, undo/redo, badges, zone rule and
mapping files (docs/ARCHITECTURE.md §13.1, AGENT_PROMPT §7, §14 tests 1-9).

The exact parity with the reference engine is checked by ``test_mapping_golden.py`` (golden files +
the live engine) and ``test_mapping_random.py`` (differential tests); this module checks the
module's own contract: shapes of every root kind, the deliberate deviations (insertion key order,
exact numbers, redo, the undo limit, the bounded tree), the zone boundaries, refusal reasons,
deep data without recursion, snapshots, the mapping file format and every validation error.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from golden import make_goldens
from golden import reference as R
from jsonreshape import codec, i18n, schema
from jsonreshape import mapping as M
from jsonreshape.errors import Cancelled, UserInputError
from jsonreshape.mapping import WILD, Designer, MapNode, MappingError, MappingTree, infer_tree
from jsonreshape.tasks import CancelToken, Progress

import jsongen

NOLIMIT = {"max_keys": None, "max_depth": None, "max_nodes": None}
SAMPLE = make_goldens.SAMPLE


def tree_of(values: Any, **kw: Any) -> MappingTree:
    return infer_tree(values, **{**NOLIMIT, **kw})


def designer_of(records: list[Any], *, undo_limit: int = 1000, **kw: Any) -> Designer:
    tree = tree_of(records, **kw)
    return Designer(tree, population=lambda: iter(records), undo_limit=undo_limit, **NOLIMIT)


def outline(tree: MappingTree) -> list[str]:
    """``depth label kind <- src`` lines (labels: root, [] for elements, keys)."""
    lines = []
    for n, _p, d in tree.walk():
        label = "(root)" if n.is_root else "[]" if n.is_elem else n.key
        flags = (" synthetic" if n.synthetic else "") + (f" computed={n.computed}" if n.computed else "") + \
                (f" rec={n.rec_key}" if n.rec_key else "")
        src = "" if n.src is None else " <- " + M.path_label(n.src, root="")
        lines.append("  " * d + f"{label} {n.kind}{flags}{src}")
    return lines


def output_of(tree: MappingTree, data: Any) -> Any:
    """The reference evaluation of ``tree`` (golden/reference.py)."""
    return R.transform(R.from_mapping_tree(tree), data)


# ----------------------------------------------------------------------------------
# model and constants
# ----------------------------------------------------------------------------------
def test_mapnode_fields_follow_the_reference_order() -> None:
    names = [f.name for f in dataclasses.fields(MapNode)]
    assert names == ["id", "key", "kind", "src", "children", "types", "count", "pcount", "samples", "synthetic",
                     "rec_key", "computed", "ctx", "open", "is_elem", "is_root"]
    n = MapNode(7)
    assert (n.key, n.kind, n.src, n.children, n.types, n.count, n.pcount, n.samples) == ("", "value", (), [], (), 0, None, ())
    assert (n.synthetic, n.rec_key, n.computed, n.ctx, n.open, n.is_elem, n.is_root) == (False, None, None, None,
                                                                                         False, False, False)
    assert MapNode(1) != MapNode(1)  # identity semantics (eq=False): nodes are compared by object
    assert WILD is None
    assert [f.name for f in dataclasses.fields(MappingTree)][0] == "root"


def test_limits_are_the_schema_limits() -> None:
    assert (M.MAX_KEYS, M.MAX_DEPTH, M.MAX_NODES) == (schema.MAX_KEYS, schema.MAX_DEPTH, schema.MAX_FIELDS)


def test_js_trim_uses_the_ecmascript_set() -> None:
    assert M.js_trim("\u001cz\u0085") == "\u001cz\u0085"  # Python's strip() would remove both
    assert M.js_trim(chr(0xFEFF) + "y" + chr(0x3000)) == "y"  # ... and keep the BOM
    head = " " + "".join(map(chr, (9, 10, 13, 11, 12)))
    tail = "".join(map(chr, (0xA0, 0x1680, 0x2000, 0x200A, 0x2028, 0x2029, 0x202F, 0x205F)))
    assert M.js_trim(head + "x" + tail) == "x"
    assert M.js_trim(chr(0x200B) + "x" + chr(0x200B)) == chr(0x200B) + "x" + chr(0x200B)
    assert M.js_trim("o'zgarish") == "o'zgarish"
    assert set(M.JS_WS) == set(R.JS_WS)


def test_type_of() -> None:
    assert [M.type_of(v) for v in ({}, [], "s", 1, 1.5, True, None)] == [
        "object", "array", "string", "number", "number", "boolean", "null"]
    assert M.type_of(codec.loads(b"1.10")) == "number" and M.type_of(codec.loads(b"-0")) == "number"
    assert M.type_of((1, 2)) == "array"
    with pytest.raises(TypeError):
        M.type_of({1, 2})


def test_path_label(lang_uz) -> None:
    assert M.path_label((WILD, "contact", "address", "city")) == "[].contact.address.city"
    assert M.path_label(("orders", WILD, "id")) == "orders[].id"
    assert M.path_label((WILD, WILD)) == "[][]"
    assert M.path_label(("", "x")) == "x"  # like the JavaScript (an empty first key adds no dot)
    assert M.path_label(()) == "(ildiz)"
    i18n.set_language("en")
    assert M.path_label(()) == "(root)"


@pytest.mark.parametrize("value, text", [
    ("Toshkent", '"Toshkent"'),
    ("o'zgarish", "\"o'zgarish\""),
    ('say "hi"\n', '"say \\"hi\\"\\n"'),
    ("x" * 26, '"' + "x" * 26 + '"'),  # 28 units: not cut
    ("x" * 27, '"' + "x" * 26 + "…"),  # 29 units: cut to 27 + …
    ("ш" * 40, '"' + "ш" * 26 + "…"),
    ("\U0001F600" * 3, '"' + "\U0001F600" * 3 + '"'),  # 8 units
    ("ab" + "\U0001F600" * 13, '"ab' + "\U0001F600" * 12 + "…"),  # 27 units end between two emoji
    ("a" + "\U0001F600" * 13, '"a' + "\U0001F600" * 12 + "\ud83d…"),  # cut inside a pair: the high surrogate stays
    ("a\ud800", '"a\\ud800"'),  # lone surrogate escaped like JSON.stringify
    (True, "true"), (False, "false"), (5, "5"), (-12, "-12"), (2.5, "2.5"),
    (12345678901234567890123456789, "1234567890123456789012345678"[:27] + "…"),
])
def test_sample_text(value: Any, text: str) -> None:
    assert M.sample_text(value) == text


def test_sample_text_keeps_exact_numbers() -> None:
    vals = codec.loads(b"[1.10, 1e400, 12345678901234567890, -0.0, 0.1, -0, 1E5]")
    assert [M.sample_text(v) for v in vals] == ["1.10", "1e400", "12345678901234567890", "-0.0", "0.1", "-0", "1E5"]


# ----------------------------------------------------------------------------------
# inference
# ----------------------------------------------------------------------------------
def test_infer_sample_tree_shape() -> None:
    """engine_map §4: first-seen key order over the whole pre-order walk (statusComment after
    children at the 4th level), an elem for the always-empty innermost children."""
    tree = tree_of(SAMPLE)
    assert outline(tree) == [
        "(root) array <- ",
        "  [] object <- []",
        "    id value <- [].id",
        "    name value <- [].name",
        "    statusComment value <- [].statusComment",
        "    contact object <- [].contact",
        "      phone value <- [].contact.phone",
        "      address object <- [].contact.address",
        "        city value <- [].contact.address.city",
        "        street value <- [].contact.address.street",
        "    children array <- [].children",
        "      [] object <- [].children[]",
        "        id value <- [].children[].id",
        "        name value <- [].children[].name",
        "        statusComment value <- [].children[].statusComment",
        "        children array <- [].children[].children",
        "          [] object <- [].children[].children[]",
        "            id value <- [].children[].children[].id",
        "            name value <- [].children[].children[].name",
        "            children array <- [].children[].children[].children",
        "              [] object <- [].children[].children[].children[]",
        "                id value <- [].children[].children[].children[].id",
        "                name value <- [].children[].children[].children[].name",
        "                children array <- [].children[].children[].children[].children",
        "                  [] value <- [].children[].children[].children[].children[]",
        "                statusComment value <- [].children[].children[].children[].statusComment",
    ]
    nodes = list(tree.iter_nodes())
    assert [n.id for n in nodes] == list(range(1, len(nodes) + 1))  # pre-order ids from 1
    assert tree.next_id == len(nodes) + 1
    assert tree.known_paths == frozenset(n.src for n in nodes)
    assert tree.root_kind == "records" and not tree.truncated
    elem = tree.find("[]")
    assert (tree.root.count, elem.count, tree.root.types, elem.types) == (1, 2, ("array",), ("object",))
    sc = tree.find("[].statusComment")
    assert (sc.count, sc.pcount, sc.types, sc.samples) == (2, 2, ("string", "null"), ('"tekshirilgan"',))
    empty = tree.find("[].children[].children[].children[].children[]")
    assert (empty.kind, empty.types, empty.count) == ("value", (), 0)
    street = tree.find("[].contact.address.street")
    assert (street.count, street.pcount) == (1, 2)


def test_infer_open_flags() -> None:
    small = tree_of(SAMPLE)  # < 160 nodes: everything above depth 6 open
    assert all(n.open == (d < 6) for n, _p, d in small.walk())
    big_record = {f"f{i}": {"a": 1, "b": [{"c": {"d": {"e": 1}}}]} for i in range(40)}
    big = tree_of([big_record])
    assert big.node_count() >= 160
    for n, _p, d in big.walk():
        assert n.open == (d <= 1 or (n.is_elem and d <= 3)), (n, d)


def test_inference_kind_rules() -> None:
    tree = tree_of([{"f": {"a": 1}, "g": 1, "h": []}, {"f": [1], "g": {"x": 1}, "h": []}])
    assert tree.find("[].f").kind == "array"  # array beats object (Ex 20)
    assert tree.find("[].f[]").kind == "value"
    assert tree.find("[].g").kind == "object" and tree.find("[].g").types == ("object", "number")
    h = tree.find("[].h")
    assert h.kind == "array" and len(h.children) == 1 and h.children[0].types == ()  # always empty
    assert [c.key for c in tree.find("[]").children] == ["f", "g", "h"]


def test_inference_keeps_insertion_key_order() -> None:
    """Deviation: JavaScript would put the array-index keys "2" and "10" first."""
    tree = tree_of([{"b": 1, "10": 2, "a": 3, "2": 4}])
    assert [c.key for c in tree.find("[]").children] == ["b", "10", "a", "2"]
    ref = R.build_schema(R.js_order([{"b": 1, "10": 2, "a": 3, "2": 4}]))
    assert [c["key"] for c in ref["children"][0]["children"]] == ["2", "10", "b", "a"]


def test_inference_samples_dedup_and_exact_numbers() -> None:
    recs = codec.loads(b'[{"v": 1.10}, {"v": 1.10}, {"v": 1e400}, {"v": null}, {"v": 12345678901234567890},'
                       b' {"v": 4}, {"s": "o\'zgarish"}, {"s": "\xd0\xa2\xd0\xbe\xd1\x88\xd0\xba\xd0\xb5\xd0\xbd\xd1\x82"}]')
    tree = tree_of(recs)
    assert tree.find("[].v").samples == ("1.10", "1e400", "12345678901234567890")
    assert tree.find("[].v").types == ("number", "null")
    assert tree.find("[].s").samples == ("\"o'zgarish\"", '"Тошкент"')


def test_infer_value_root_kinds() -> None:
    assert outline(tree_of([5], root_kind="value")) == ["(root) value <- "]
    t = tree_of([[{"a": 1}, {"a": 2}]], root_kind="value")
    assert outline(t) == ["(root) array <- ", "  [] object <- []", "    a value <- [].a"]
    t = tree_of([{"meta": {"v": 1}, "data": [{"id": 1}]}], root_kind="value")
    assert t.root.kind == "object" and t.find("data[]").src == ("data", WILD)
    assert tree_of([], root_kind="value").root.kind == "value"
    assert outline(tree_of([])) == ["(root) array <- ", "  [] value <- []"]


def test_infer_keyed_root_merges_member_values(lang_uz) -> None:
    members = {"child1": {"child1.2": {"id": 112}}, "child2": {"id": 2}, "c3": {"id": 3, "x": [1]}}
    tree = tree_of(list(members.values()), root_kind="keyed")
    assert tree.root_kind == "keyed" and tree.root.count == 3 and tree.root.kind == "object"
    assert outline(tree) == ["(root) object <- ", "  child1.2 object <- child1.2", "    id value <- child1.2.id",
                             "  id value <- id", "  x array <- x", "    [] value <- x[]"]
    assert M.node_label(tree, tree.root, file_name="a.json") == "har bir a'zo"
    # identity: every member value maps to itself (keys kept by the engine)
    assert {k: output_of(tree, v) for k, v in members.items()} == members
    assert M.node_meta(tree, tree.root, record_count=3) == [("3 ta a'zo", "")]


def test_infer_wrapped_root(lang_uz) -> None:
    wrapper = {"meta": {"v": 1, "tags": ["a"]}, "data": [], "tail": {"n": 2}}
    records = ({"id": i, "name": f"n{i}"} for i in range(3))  # a generator, consumed in place
    tree = infer_tree(records, root_kind="wrapped", wrapper=wrapper, record_path=("data",))
    assert outline(tree) == [
        "(root) object <- ", "  meta object <- meta", "    v value <- meta.v", "    tags array <- meta.tags",
        "      [] value <- meta.tags[]", "  data array <- data", "    [] object <- data[]", "      id value <- data[].id",
        "      name value <- data[].name", "  tail object <- tail", "    n value <- tail.n"]
    assert tree.record_src == ("data",) and tree.find("data[]").count == 3 and tree.find("data").count == 1
    assert wrapper == {"meta": {"v": 1, "tags": ["a"]}, "data": [], "tail": {"n": 2}}  # not modified
    assert M.node_label(tree, tree.find("data[]")) == "har bir yozuv"
    assert M.node_label(tree, tree.find("meta.tags[]")) == "har bir element"
    # a record array inside an array element (record path with an index)
    w2 = {"pages": [{"items": [], "p": 1}, {"items": [{"id": 9, "z": 1}]}]}
    t2 = infer_tree([{"id": 1}], root_kind="wrapped", wrapper=w2, record_path=("pages", 0, "items"))
    assert t2.record_src == ("pages", WILD, "items")
    assert [c.key for c in t2.find("pages[].items[]").children] == ["id", "z"]
    assert t2.find("pages[].items[]").count == 2
    with pytest.raises(ValueError):
        infer_tree([], root_kind="wrapped", wrapper={"a": 1}, record_path=("data",))
    with pytest.raises(ValueError):
        infer_tree([], root_kind="wrapped", wrapper={"a": 1})
    with pytest.raises(ValueError):
        infer_tree([], root_kind="table")


def test_wrapped_and_keyed_trees_from_real_documents(tmp_path: Path) -> None:
    from jsonreshape.reader import Document

    recs = [{"id": i, "name": f"n{i}", "tags": ["x"] * (i % 2)} for i in range(5)]
    p = tmp_path / "w.json"
    p.write_bytes(jsongen.build_document(recs, "wrapped"))
    with Document.open(p) as doc:
        doc.build_index()
        assert doc.info.kind == "wrapped"
        prefix, suffix = doc.wrapper_parts()
        wrapper = codec.loads(prefix + b"[]" + suffix)
        values = [doc.record(i) for i in range(doc.record_count)]
        tree = infer_tree(values, root_kind=M.root_kind_for(doc.info.kind), wrapper=wrapper,
                          record_path=doc.info.record_path)
        assert tree.root_kind == "wrapped" and tree.record_src == ("data",)
        assert [c.key for c in tree.root.children] == ["meta", "data", "tail"]
        info = M.make_input_info(doc, sample=10000)
        assert info["kind"] == "wrapped" and info["record_path"] == ["data"] and info["records"] == 5
        assert info["source"] == str(p) and info["sample"] == 10000 and info["full_scan"] is False
        assert info["created"] and info["app"]
    p2 = tmp_path / "k.json"
    p2.write_bytes(jsongen.build_document([{"a": i, "b": {"c": i}} for i in range(4)], "keyed"))
    with Document.open(p2) as doc:
        doc.build_index()
        assert doc.info.kind == "keyed"
        tree = infer_tree([doc.record(i) for i in range(doc.record_count)], root_kind=M.root_kind_for("keyed"))
        assert outline(tree) == ["(root) object <- ", "  a value <- a", "  b object <- b", "    c value <- b.c"]


def test_root_kind_for() -> None:
    assert [M.root_kind_for(k) for k in ("array", "jsonl", "stream", "brace_list", "wrapped", "keyed", "single")] == [
        "records", "records", "records", "records", "wrapped", "keyed", "value"]
    with pytest.raises(ValueError):
        M.root_kind_for("csv")


def test_inference_limits_make_opaque_value_nodes(lang_uz) -> None:
    wide = {"scores": {f"k{i}": i for i in range(30)}, "a": 1}
    tree = infer_tree([wide, {"scores": {"z": 1}}], max_keys=10)
    scores = tree.find("[].scores")
    assert scores.kind == "value" and scores.types == ("object",) and not scores.children and tree.truncated
    assert ("butunicha", "Kalitlari juda ko'p yoki juda chuqur: qiymat o'zgarishsiz ko'chiriladi", "muted") in \
        M.badges(tree, scores)
    assert output_of(tree, [wide]) == [wide]  # copied whole: nothing lost
    deep = {"a": {"b": {"c": {"d": {"e": 1}}}}}
    t2 = infer_tree([deep], max_depth=3)
    assert t2.truncated and t2.find("[].a.b").kind == "value" and t2.find("[].a.b").types == ("object",)
    assert max(len(n.src) for n in t2.iter_nodes()) == 3
    assert output_of(t2, [deep]) == [deep]
    t3 = infer_tree([{f"f{i}": {"x": i} for i in range(50)}], max_nodes=20)
    assert t3.truncated and t3.node_count() <= 20 + 2
    assert output_of(t3, [{f"f{i}": {"x": i} for i in range(50)}]) == [{f"f{i}": {"x": i} for i in range(50)}]
    normal = infer_tree(SAMPLE)  # the default limits do not change ordinary trees
    assert outline(normal) == outline(tree_of(SAMPLE)) and not normal.truncated
    for bad in ({"max_keys": 0}, {"max_depth": 0}, {"max_nodes": 1}, {"max_keys": True}, {"max_depth": 1.5}):
        with pytest.raises(ValueError):
            infer_tree([], **bad)


def test_inference_progress_and_cancel() -> None:
    seen: list[int] = []
    prog = Progress(unit="records", callback=lambda info: seen.append(info.done), min_interval=0)
    tree = infer_tree(({"i": i} for i in range(1000)), progress=prog)
    assert tree.find("[]").count == 1000 and seen and seen[-1] == 1000
    tok = CancelToken()
    tok.cancel()
    with pytest.raises(Cancelled):
        infer_tree(({"i": i} for i in range(10000)), cancel=tok)
    with pytest.raises(Cancelled):
        infer_tree([{"x": list(range(100000))}], cancel=tok)


def _no_floats(v: Any) -> Any:
    """Floats -> ints (the reference spells samples of floats as JavaScript doubles)."""
    if isinstance(v, dict):
        return {k: _no_floats(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_no_floats(x) for x in v]
    if isinstance(v, float):
        return int(v) if v == v and abs(v) != float("inf") else 0
    return v


@pytest.mark.parametrize("seed", range(4))
def test_inference_equals_the_reference_on_generated_records(seed: int) -> None:
    recs = R.js_order(_no_floats(json.loads(json.dumps(jsongen.records(60, seed=seed), default=str))))
    ours = make_goldens.canon_tree(R.from_mapping_tree(tree_of(recs)))
    assert ours == make_goldens.canon_tree(R.build_schema(recs))


# ----------------------------------------------------------------------------------
# navigation
# ----------------------------------------------------------------------------------
def test_find_parent_and_refs() -> None:
    d = designer_of([{"a": 1, "orders": [{"oid": 1}], "k": [[1]]}])
    tree = d.tree
    d.merge("[].k", "[].orders")  # orders gets a second element node
    assert d.add_group("[]", "a").node.key == "a_2"  # the rename collides: "a_2"
    tree.root.children[0].children.append(MapNode(999, key="a", src=(WILD, "a")))  # a real duplicate key
    tree.touch()
    assert tree.find("") is tree.root and tree.find("[]") is tree.root.children[0]
    assert tree.find("[].orders[1]").src == (WILD, "k", WILD)
    assert tree.find(["[]"]) is None and tree.find([0, "orders", 0]).src == (WILD, "orders", WILD)
    assert tree.find({"id": 999}).id == 999 and tree.find(999) is tree.find({"id": 999})
    for bad in ("[].nope", "[]..a", "a[x]", "[5]", [True], [0, 9], {"id": "1"}, 3.5, None, True):
        assert tree.find(bad) is None, bad
    for n in tree.iter_nodes():
        ref = M.ref_of(tree, n)
        assert tree.find(ref) is n
        assert tree.parent(n) is (None if n is tree.root else next(p for p in tree.iter_nodes() if n in p.children))
    dup = tree.find(999)
    assert M.ref_of(tree, dup)[-1] == len(tree.root.children[0].children) - 1  # not the first "a": an index
    assert M.parse_ref("[].children[].name") == [0, "children", 0, "name"]
    assert M.parse_ref("orders[1][]") == ["orders", 1, 0]
    for bad in ("a..b", "a]", "[x]", "a[1"):
        with pytest.raises(ValueError):
            M.parse_ref(bad)


def test_tree_index_follows_mutations() -> None:
    tree = tree_of([{"a": 1, "b": 2}])
    elem = tree.find("[]")
    b = tree.find("[].b")
    elem.children.remove(b)
    tree.touch()
    assert tree.find("[].b") is None and tree.find(b.id) is None and not tree.contains(b)
    tree.root = MapNode(1, kind="array", is_root=True, children=[MapNode(2, src=(WILD,), is_elem=True)])
    assert tree.find(2) is tree.root.children[0]  # replacing the root resets the index


def test_stale_node_objects_resolve_by_id() -> None:
    d = designer_of([{"a": 1, "b": 2}])
    a = d.tree.find("[].a")
    d.rename(a, "x")
    d.undo()
    assert not d.tree.contains(a)  # a node object of the earlier state
    r = d.rename(a, "y")  # resolved by its id, like the prototype's data-id
    assert r.applied and d.tree.find("[].y").id == a.id


# ----------------------------------------------------------------------------------
# snapshots
# ----------------------------------------------------------------------------------
def test_snapshot_round_trip() -> None:
    d = designer_of(SAMPLE)
    d.flatten("[].children")
    d.add_group("[]", "g")
    tree = d.tree
    tree.orig = d.orig
    tree.find("[].contact").open = False
    snap = tree.snapshot()
    assert isinstance(snap, bytes)
    back = MappingTree.restore(snap)
    assert back.snapshot() == snap
    assert make_goldens.canon_tree(R.from_mapping_tree(back)) == make_goldens.canon_tree(R.from_mapping_tree(tree))
    assert [n.id for n in back.iter_nodes()] == [n.id for n in tree.iter_nodes()]
    assert (back.next_id, back.root_kind, back.known_paths, back.truncated) == (
        tree.next_id, tree.root_kind, tree.known_paths, tree.truncated)
    assert back.find("[].contact").open is False and back.orig is not None
    tree.find("[].contact").key = "changed"
    assert MappingTree.restore(snap).find("[].contact") is not None  # the bytes do not change
    for bad in (b"[]", b'{"v": 2}', b'{"v": 1, "nodes": []}', b'{"v": 1, "nodes": [[1, "", "array", [], 2]]}'):
        with pytest.raises((ValueError, IndexError, KeyError)):
            MappingTree.restore(bad)


def test_undo_restores_every_field_including_open() -> None:
    d = designer_of(SAMPLE)
    contact = d.tree.find("[].contact")
    contact.open = False  # a UI toggle: no undo step
    before = make_goldens.canon_tree(R.from_mapping_tree(d.tree)), [n.id for n in d.tree.iter_nodes()]
    d.move("[].contact.address.city", "[].name", "before")
    d.tree.find("[].contact").open = True
    d.undo()
    assert (make_goldens.canon_tree(R.from_mapping_tree(d.tree)), [n.id for n in d.tree.iter_nodes()]) == before
    assert d.tree.find("[].contact").open is False


def test_history_entries_share_unchanged_subtrees() -> None:
    d = designer_of([{f"f{i}": {"x": {"y": i}} for i in range(200)}])
    target = d.tree.find("[].f0").id
    for i in range(20):
        assert d.rename(target, f"n{i}").applied
    entries = list(d._undo)
    assert len(entries) == 20
    first, last = entries[0], entries[-1]
    # the untouched object nodes of the record element are the very same tuples in both snapshots
    a, b = first[4][0][4], last[4][0][4]
    assert sum(1 for x, y in zip(a[1:], b[1:]) if x is y) == len(a) - 1


# ----------------------------------------------------------------------------------
# designer operations
# ----------------------------------------------------------------------------------
def test_move_variants_and_messages(lang_uz) -> None:
    d = designer_of([{"name": "r", "name_2": "x", "a_2": 1, "kids": [{"name": "k1", "a_2": 2}, {"name": "k2"}]}])
    r = d.move("[].kids[].name", "[]", "inside")
    assert (r.applied, r.changed, r.message) == (True, True, "«name» nomi band edi, «name_3» deb qo'yildi")
    assert r.node is d.tree.find("[].name_3")
    r = d.move("[].kids[].a_2", "[].a_2", "after")
    assert r.message == "«a_2» nomi band edi, «a_2_2» deb qo'yildi"
    assert [c.key for c in d.tree.find("[]").children] == ["name", "name_2", "a_2", "a_2_2", "kids", "name_3"]
    r = d.move("[].a_2", "[].name_2", "after")  # already there: accepted, unchanged
    assert (r.applied, r.changed, r.message) == (True, False, "")
    assert d.steps == 3
    r = d.rename("[].kids", "name")
    assert r.message == "«name» band edi, «name_4» deb nomlandi"
    i18n.set_language("en")
    r = d.move("[].name_4", "[].name", "before")
    assert r.applied and r.message == ""
    d2 = designer_of([{"n": 1, "k": [{"m": "a"}]}])
    r = d2.merge("[].k", "")
    assert r.message == "The elements of «k» were added to the root array" and r.node is d2.tree.root
    assert len(d2.tree.root.children) == 2 and d2.tree.find("[].k") is None


def test_move_refusals_have_reasons(lang_uz) -> None:
    d = designer_of([{"id": 1, "c": {"x": {"y": 1}}, "t": [1], "o": [{"z": 1}]}])
    cases = [
        (("", "[].id", "inside"), "mapping.why.root"),
        (("[]", "[].c", "inside"), "mapping.why.elem"),
        (("[].c", "[].c.x", "inside"), "mapping.why.into_itself"),
        (("[].c", "[].c", "after"), "mapping.why.into_itself"),
        (("[].id", "[].t", "inside"), "mapping.why.array_only"),
        (("[].c", "[].id", "inside"), "mapping.why.not_object"),
        (("[].c", "[].t[]", "inside"), "mapping.why.not_object"),
        (("[].id", "[]", "before"), "mapping.why.no_sibling"),
        (("[].id", "", "after"), "mapping.why.no_sibling"),
        (("[].id", "[].c", None), "mapping.why.no_zone"),
        (("[].nope", "[].c", "inside"), "mapping.why.missing"),
    ]
    for (node, target, zone), key in cases:
        r = d.move(node, target, zone)
        assert (r.applied, r.changed, r.message, r.node) == (False, False, "", None), (node, target, zone)
        assert r.reason == i18n.t(key)
    assert d.steps == 0 and not d.can_undo
    r = d.move("[].id", "[].c", "sideways")  # any other zone acts as "before" (reference)
    assert r.applied and [c.key for c in d.tree.find("[]").children][:2] == ["id", "c"]


def test_delete_unwrap_group_rename(lang_uz) -> None:
    d = designer_of([{"city": "A", "address": {"city": "B", "zip": 1}, "o": [{"x": 1}], "p": [{"y": 1}]}])
    assert d.delete("").reason == i18n.t("mapping.why.root")
    assert d.delete("[]").reason == i18n.t("mapping.why.last_elem")
    r = d.unwrap("[].address")
    assert r.message == "«address» ichidagi 2 ta maydon bir pog'ona yuqoriga chiqdi, 1 tasining nomiga raqam qo'shildi"
    assert r.node is d.tree.find("[]")
    assert [c.key for c in d.tree.find("[]").children] == ["city", "city_2", "zip", "o", "p"]
    assert d.unwrap("[].o").reason == i18n.t("mapping.why.not_object")
    assert d.unwrap("[].o[]").reason == i18n.t("mapping.why.elem")
    d.merge("[].p", "[].o")
    r = d.delete("[].o[1]")
    assert r.message == "«element» natijadan olib tashlandi" and r.node.is_elem
    assert d.delete("[].o[0]").reason == i18n.t("mapping.why.last_elem")
    r = d.delete("[].zip")
    assert r.message == "«zip» natijadan olib tashlandi"
    r = d.add_group("[]")
    assert (r.applied, r.message, r.node.key, r.node.synthetic, r.node.src, r.node.types, r.node.open) == (
        True, "", "yangi_guruh", True, None, ("object",), True)
    steps = d.steps
    r = d.add_group("[]", "  holat" + chr(0x3000))
    assert r.node.key == "holat" and d.steps == steps + 2  # group + rename
    r = d.add_group("[]", "yangi_guruh_2")  # the rename is refused (same name): one step
    assert r.node.key == "yangi_guruh_2" and d.steps == steps + 3 and r.message == ""
    assert d.add_group("[].city").reason == i18n.t("mapping.why.not_object")
    assert d.add_group("[].holat").node.key == "yangi_guruh"  # nested groups
    assert d.rename("[].city", "  ").reason == i18n.t("mapping.why.empty_name")
    assert d.rename("[].city", " city ").reason == i18n.t("mapping.why.same_name")
    assert d.rename("[]", "x").reason == i18n.t("mapping.why.elem")
    assert d.rename("", "x").reason == i18n.t("mapping.why.root")
    r = d.rename("[].city", chr(0xFEFF) + "shahar" + chr(0x2028))
    assert r.applied and r.node.key == "shahar" and r.message == ""
    i18n.set_language("en")
    assert d.add_group("[]").node.key == "new_group"
    r = d.unwrap("[].holat")  # its nested group meets the first group's name
    assert r.message == "Fields moved one level up from «holat»: 1 (renamed with a number: 1)"


def test_unwrap_message_singular_in_english(lang_en) -> None:
    d = designer_of([{"a": {"b": 1}}])
    assert d.unwrap("[].a").message == "1 field of «a» moved one level up"


def test_actions_follow_the_prototype_rules(lang_uz) -> None:
    d = designer_of([{"v": 1, "o": {"x": 1}, "a": [{"c": [{"z": 1}]}], "s": ["t"]}])
    d.add_group("[]", "g")
    acts = {ref: d.actions(ref) for ref in ("", "[]", "[].v", "[].o", "[].a", "[].a[]", "[].s[]", "[].g")}
    enabled = {ref: sorted(k for k, v in a.items() if v is None) for ref, a in acts.items()}
    assert enabled == {
        "": ["flatten"], "[]": ["group"], "[].v": ["delete", "rename"], "[].o": ["delete", "group", "rename", "unwrap"],
        "[].a": ["delete", "flatten", "rename"], "[].a[]": ["group"], "[].s[]": [],
        "[].g": ["delete", "group", "rename", "unwrap"]}
    assert acts["[].a[]"]["delete"] == i18n.t("mapping.why.last_elem")
    assert acts["[].v"]["group"] == i18n.t("mapping.why.not_object")
    assert d.actions("[].missing")["rename"] == i18n.t("mapping.why.missing")
    assert d.can_drag("[].v") and not d.can_drag("") and not d.can_drag("[]")


def test_flatten_with_plan_and_population(lang_uz) -> None:
    recs = [{"name": "p1", "children": [{"name": "c1", "children": [{"name": "g1", "extra": 1}]}]}]
    tree = tree_of(recs)
    d = Designer(tree, undo_limit=300)  # no population: nothing to re-infer from
    assert d.can_flatten("[].children") and not d.can_flatten("[].name") and not d.can_flatten("[]")
    r = d.flatten("[].children")
    assert not r.applied and r.reason == i18n.t("mapping.why.no_data")
    assert d.plan_flatten("[].children") is None
    d.population = lambda: iter(recs)
    plan = d.plan_flatten("[].children")
    assert plan is not None and plan.entries[0][1] == "children" and plan.entries[0][3] == 2
    d.rename("[].name", "nom")  # the tree changed: the plan is stale
    assert d.flatten("[].children", plan=plan).reason == i18n.t("mapping.why.stale")
    plan = d.plan_flatten("[].children")
    known_before = d.tree.known_paths
    r = d.flatten("[].children", plan=plan)
    assert r.applied and r.message == "«children» ichidagi barcha darajalar bitta massivga yig'ildi: 2 ta element"
    assert r.node is d.tree.find("[].children")
    assert [c.key for c in d.tree.find("[].children[]").children] == ["_depth", "name", "extra"]
    assert (WILD, "children", WILD, "extra") in d.tree.known_paths and known_before < d.tree.known_paths
    assert not d.can_flatten("[].children")
    assert d.flatten("[].children").reason == i18n.t("mapping.why.not_flattenable")
    assert d.flatten("[].nope").reason == i18n.t("mapping.why.missing")
    assert output_of(d.tree, recs) == [{"nom": "p1", "children": [{"_depth": 1, "name": "c1"},
                                                                  {"_depth": 2, "name": "g1", "extra": 1}]}]


def test_flatten_progress_and_cancel(lang_uz) -> None:
    recs = [{"children": [{"n": i, "children": [{"n": j} for j in range(30)]} for i in range(30)]} for _ in range(20)]
    d = designer_of(recs)
    tok = CancelToken()
    tok.cancel()
    with pytest.raises(Cancelled):
        d.flatten("[].children", cancel=tok)
    assert d.steps == 0 and d.tree.find("[].children[]").rec_key is None
    seen: list[int] = []
    prog = Progress(unit="items", callback=lambda info: seen.append(info.done), min_interval=0)
    r = d.flatten("[].children", progress=prog)
    assert r.applied and r.message.endswith(f"{20 * (30 + 30 * 30)} ta element") and seen


def test_flatten_other_root_kinds(lang_uz) -> None:
    members = [{"name": "a", "kids": [{"name": "b", "kids": [{"name": "c"}]}]}, {"name": "d"}]
    tree = tree_of(members, root_kind="keyed")
    d = Designer(tree, population=lambda: iter(members), undo_limit=300, **NOLIMIT)
    r = d.flatten("kids")
    assert r.applied and r.message.endswith(": 2 ta element")
    assert output_of(d.tree, members[0]) == {"name": "a", "kids": [{"_depth": 1, "name": "b"}, {"_depth": 2, "name": "c"}]}
    doc = {"meta": {"tree": [{"n": 1, "sub": [{"n": 2}]}]}, "data": [{"x": [{"y": 1, "x": [{"y": 2}]}]}]}
    wrapper = {"meta": doc["meta"], "data": []}
    wt = infer_tree(doc["data"], root_kind="wrapped", wrapper=wrapper, record_path=["data"], **NOLIMIT)
    wd = Designer(wt, population=lambda: iter(doc["data"]), wrapper=wrapper, record_path=["data"], undo_limit=300)
    assert wd.flatten("meta.tree").message.endswith(": 2 ta element")
    assert wd.flatten("data[].x").message.endswith(": 2 ta element")
    assert output_of(wd.tree, doc) == {"meta": {"tree": [{"_depth": 1, "n": 1}, {"_depth": 2, "n": 2}]},
                                       "data": [{"x": [{"_depth": 1, "y": 1}, {"_depth": 2, "y": 2}]}]}
    vt = tree_of([doc], root_kind="value")
    vd = Designer(vt, population=lambda: iter([doc]), undo_limit=300)
    assert vd.flatten("meta.tree").applied


def test_zone_for_matrix_and_exact_boundaries() -> None:
    d = designer_of([{"id": 1, "contact": {"city": "T"}, "tags": ["x"], "kids": [{"n": 1, "kids": [{"n": 2}]}],
                      "grid": [[1]]}])
    z = d.zone_for
    for frac, want in ((0.0, "before"), (0.27, "before"), (0.2799, "before"), (0.28, "inside"), (0.5, "inside"),
                       (0.72, "inside"), (0.7201, "after"), (0.73, "after"), (1.0, "after")):
        assert z("[].contact", "[].id", frac) == want, frac  # object target, value dragged
        assert z("[].kids", "[].kids[].kids", frac) == want, frac  # array onto array: inside = merge
    for frac, want in ((0.0, "before"), (0.4999, "before"), (0.5, "after"), (1.0, "after")):
        assert z("[].id", "[].contact", frac) == want  # value rows: never inside
        assert z("[].kids", "[].id", frac) == want  # a non-array onto an array row
    for frac in (0.0, 0.5, 1.0):
        assert z("[]", "[].id", frac) == "inside"  # element rows: inside on the whole row
        assert z("[].grid[]", "[].kids", frac) == "inside"  # an array onto an element of arrays
        assert z("[].grid[]", "[].id", frac) is None
        assert z("[].tags[]", "[].kids", frac) is None  # element of scalars: no drop
        assert z("", "[].id", frac) is None  # root array: only arrays
        assert z("", "[].kids", frac) == "inside"
        assert z("[].contact.city", "[].contact", frac) is None  # into its own descendant
        assert z("[].contact", "[].contact", frac) is None  # onto itself
    assert z("[].missing", "[].id", 0.5) is None and z("[].id", None, 0.5) is None


# ----------------------------------------------------------------------------------
# undo / redo / reset
# ----------------------------------------------------------------------------------
def test_undo_redo_reset_sequences(lang_uz) -> None:
    d = designer_of([{"a": 1, "b": {"c": 2}}])
    assert (d.can_undo, d.can_redo, d.steps, d.changes, d.redo_steps) == (False, False, 0, 0, 0)
    assert d.undo().reason == i18n.t("mapping.why.nothing_to_undo")
    assert d.redo().reason == i18n.t("mapping.why.nothing_to_redo")
    v0 = d.version
    d.rename("[].a", "A")
    d.unwrap("[].b")
    assert d.steps == 2 and d.version == v0 + 2
    r = d.undo()
    assert (r.applied, r.changed, r.message) == (True, True, "Oxirgi o'zgarish bekor qilindi")
    assert d.tree.find("[].b") is not None and d.can_redo and d.redo_steps == 1
    r = d.redo()
    assert (r.applied, r.message) == (True, "Bekor qilingan o'zgarish qayta bajarildi")
    assert d.tree.find("[].b") is None and d.tree.find("[].c") is not None and d.steps == 2
    d.undo()
    d.undo()
    assert d.steps == 0 and d.redo_steps == 2
    d.rename("[].a", "Z")  # a new step clears the redo stack
    assert d.redo_steps == 0 and not d.can_redo
    r = d.reset()
    assert (r.applied, r.changed, r.message) == (True, True, "Asl tuzilma tiklandi")
    assert d.tree.find("[].a") is not None and d.steps == 2
    r = d.reset()  # always a step, even when nothing changes
    assert (r.applied, r.changed) == (True, False) and d.steps == 3
    d.undo()
    d.undo()
    assert d.tree.find("[].Z") is not None
    assert M.stats_text(d.tree, d.changes) == "3 maydon · 1 o'zgarish"


def test_undo_limit_and_settings_default() -> None:
    d = designer_of([{"a": 1}], undo_limit=200)
    for i in range(205):
        assert d.rename([0, 0], f"k{i}").applied
    assert d.steps == 200
    undone = 0
    while d.undo().applied:
        undone += 1
    assert undone == 200 and d.tree.find([0, 0]).key == "k4"
    assert Designer(tree_of([{"a": 1}])).undo_limit == 1000  # Settings.undo_limit
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            Designer(tree_of([{"a": 1}]), undo_limit=bad)


def test_ids_never_go_back() -> None:
    d = designer_of([{"a": 1}])
    g1 = d.add_group("[]").node.id
    d.undo()
    g2 = d.add_group("[]").node.id
    assert g2 > g1 and d.tree.next_id == g2 + 1
    d.reset()
    assert d.add_group("[]").node.id > g2


# ----------------------------------------------------------------------------------
# deep data (iterative everywhere)
# ----------------------------------------------------------------------------------
def _chain(depth: int) -> dict:
    node: dict = {"name": f"n{depth}", "children": []}
    for i in range(depth - 1, 0, -1):
        node = {"name": f"n{i}", "children": [node]}
    return node


def test_deep_recursive_data_without_recursion_errors(tmp_path: Path, lang_uz) -> None:
    depth = 2000
    rec = _chain(depth)
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(200, min(old, 1000)))
    try:
        tree = infer_tree([rec], **NOLIMIT)
        assert max(len(n.src) for n in tree.iter_nodes()) == 2 * depth + 1  # the always-empty innermost elem
        deepest = max((n for n in tree.iter_nodes() if not n.is_elem), key=lambda n: len(n.src))
        assert tree.find(M.ref_of(tree, deepest)) is deepest
        M.badges(tree, deepest)
        snap = tree.snapshot()
        assert MappingTree.restore(snap).snapshot() == snap
        d = Designer(tree, population=lambda: iter([rec]), undo_limit=300, **NOLIMIT)
        assert d.rename(M.ref_of(tree, deepest), "deep").applied
        r = d.flatten("[].children")
        assert r.applied and r.message.endswith(f": {depth - 1} ta element")
        assert [c.key for c in d.tree.find("[].children[]").children] == ["_depth", "name"]
        d.undo()
        again = max((n for n in d.tree.iter_nodes() if not n.is_elem), key=lambda n: len(n.src))
        assert d.tree.find(M.ref_of(d.tree, again)).key == "deep"
        assert d.redo().applied and d.undo().applied
        assert len(d.tree.snapshot()) < 2_000_000  # src stored relative to the parent: linear size
        # the default limits keep deep data bounded (opaque below 128 levels) and still flattenable
        limited = infer_tree([rec])
        assert limited.truncated and max(len(n.src) for n in limited.iter_nodes()) <= M.MAX_DEPTH
        d2 = Designer(limited, population=lambda: iter([rec]), undo_limit=300)
        assert d2.flatten("[].children").applied and d2.undo().applied
        path = tmp_path / "deep.mapping.json"
        M.save_mapping(d2.tree, path, input_info={"kind": "array"})
        loaded, _info = M.load_mapping(path)
        assert M.node_to_dict(loaded.root) == M.node_to_dict(d2.tree.root)
        # a moderately deep unlimited tree through the file (absolute src paths: depth x nodes)
        mid = infer_tree([_chain(300)], **NOLIMIT)
        d3 = Designer(mid, population=lambda: iter([_chain(300)]), undo_limit=300, **NOLIMIT)
        d3.flatten("[].children")
        M.save_mapping(d3.tree, path, input_info={"kind": "array"}, orig=d3.orig)
        loaded, _info = M.load_mapping(path)
        assert M.node_to_dict(loaded.root) == M.node_to_dict(d3.tree.root)
        assert M.node_to_dict(loaded.orig.root) == M.node_to_dict(d3.orig.root)
    finally:
        sys.setrecursionlimit(old)


def test_deep_mapping_file_beyond_the_json_decoders() -> None:
    """A tree too deep for the C decoders (nested groups: linear size) is read iteratively."""
    depth = 30000
    root = MapNode(1, kind="object", is_root=True)
    cur = root
    for i in range(depth):
        g = MapNode(i + 2, key=f"g{i}", kind="object", src=None, synthetic=True, types=("object",))
        cur.children.append(g)
        cur = g
    data = M.dumps_mapping(MappingTree(root, root_kind="value"), input_info={})
    with pytest.raises(codec.JsonDecodeError):
        codec.loads(data)
    loaded, _ = M.load_mapping(data)
    assert loaded.node_count() == depth + 1
    assert [n.key for n in loaded.iter_nodes()][-1] == f"g{depth - 1}"


# ----------------------------------------------------------------------------------
# badges and row texts
# ----------------------------------------------------------------------------------
def test_badges_order_tooltips_and_roles(lang_uz) -> None:
    d = designer_of([{"name": "A", "orders": [{"oid": 7, "items": [{"sku": "x"}], "kids": [{"k": 1, "kids": [{"k": 2}]}]}],
                      "list": [{"z": 1}]}])
    d.move("[].orders[].oid", "[].name", "after")  # gathered, moved
    d.rename("[].oid", "ids")
    d.move("[].name", "[].orders[]", "inside")  # broadcast
    d.add_group("[]", "g")
    d.flatten("[].orders[].kids")
    d.merge("[].list", "[].orders[].items")
    t = d.tree
    b = {M.path_label(M.ref_of(t, n), root="") if False else repr(M.ref_of(t, n)): M.badges(t, n)
         for n in t.iter_nodes() if M.badges(t, n)}
    assert b == {
        "[0, 'ids']": [("ko'chirildi", "Asl joyi: [].orders[].oid", "accent"), ("asl nomi: oid", "", "muted"),
                       ("ro'yxatga yig'iladi",
                        "Massiv ichidan olingan: har bir elementdagi qiymat bitta ro'yxatga yig'iladi", "warn")],
        "[0, 'orders', 0, 'items', 1]": [("manba: [].list", "Bu elementlar boshqa massivdan qo'shilgan", "array")],
        "[0, 'orders', 0, 'kids', 0]": [("rekursiv · kids", "«kids» ichidagi barcha darajalar shu massivga tushadi",
                                         "ok")],
        "[0, 'orders', 0, 'kids', 0, '_depth']": [
            ("daraja raqami", "Element qaysi darajadan kelgani: 1 = bevosita, 2 = uning ichidagisi…", "muted")],
        "[0, 'orders', 0, 'name']": [("ko'chirildi", "Asl joyi: [].name", "accent"), (
            "har elementga takrorlanadi",
            "Massivdan tashqaridan olingan: har bir elementga bir xil qiymat yoziladi", "bool")],
        "[0, 'g']": [("yangi guruh", "", "accent")],
    }
    i18n.set_language("en")
    assert [x[0] for x in M.badges(t, t.find("[].ids"))] == ["moved", "original name: oid", "collected into a list"]


def test_relation_and_is_moved() -> None:
    n = MapNode(1, key="v", src=(WILD, "o", WILD, "v"))
    assert M.relation(n, [(WILD,)]) == (True, False)  # gathered at record level
    assert M.relation(n, [(WILD,), (WILD, "o", WILD)]) == (False, False)  # in place
    assert M.relation(MapNode(2, key="n", src=(WILD, "n")), [(WILD,), (WILD, "o", WILD)]) == (False, True)
    assert M.relation(MapNode(3, synthetic=True, src=None), [(WILD,)]) == (False, False)
    p = MapNode(4, key="o", kind="object", src=(WILD, "o"))
    assert not M.is_moved(MapNode(5, key="x", src=(WILD, "o", "x")), p)
    assert M.is_moved(MapNode(5, key="x", src=(WILD, "x")), p)
    assert M.is_moved(MapNode(5, key="x", src=(WILD, "o", "x")), MapNode(6, kind="object", synthetic=True, src=None))
    assert not M.is_moved(MapNode(5, key="x", src=(WILD, "x")), None)


def test_row_texts(lang_uz) -> None:
    recs = [{"id": i, "opt": 1} if i % 8 == 0 else {"id": i} for i in range(12345)]
    tree = infer_tree(recs + [{"e": []}])
    elem = tree.find("[]")
    assert M.node_label(tree, tree.root, file_name="data.json") == "data.json"
    assert M.node_label(tree, tree.root) == "ildiz"
    assert M.node_label(tree, elem) == "har bir yozuv" and M.node_label(tree, tree.find("[].e[]")) == "har bir element"
    assert M.node_label(tree, tree.find("[].opt")) == "opt"
    assert M.node_meta(tree, elem) == [("12 346 ta", "")]
    assert M.node_meta(tree, tree.root, record_count=12346) == [("12 346 ta yozuv", "")]
    assert M.node_meta(tree, tree.find("[].opt")) == [("13%", "1544 / 12346 ta obyektda bor")]
    # 99.99 % rounds to "100%" but is still shown: the field is missing in one object
    assert M.node_meta(tree, tree.find("[].id")) == [("100%", "12345 / 12346 ta obyektda bor")]
    half = infer_tree([{"a": 1}, {}, {"a": 2}, {}, {"a": 3}, {}, {"a": 4}, {}])
    assert M.node_meta(half, half.find("[].a"))[0][0] == "50%"
    eighth = infer_tree([{"a": 1}] + [{}] * 7)
    assert M.node_meta(eighth, eighth.find("[].a"))[0][0] == "13%"  # Math.round(12.5) = 13
    assert M.type_chips(tree.find("[].e[]")) == [("bo'sh", "massiv doim bo'sh", "null")]
    assert M.type_chips(tree.find("[].id")) == [("123", "son", "number")]
    mixed = infer_tree([{"m": {}}, {"m": []}, {"m": "s"}, {"m": 1}, {"m": True}, {"m": None}])
    assert [c[0] for c in M.type_chips(mixed.find("[].m"))] == ["{ }", "[ ]", "abc", "123", "t/f", "null"]
    assert [c[2] for c in M.type_chips(mixed.find("[].m"))] == ["object", "array", "string", "number", "bool", "null"]
    assert M.sample_display(tree.find("[].id")) == "0, 1, 2"
    assert M.sample_display(elem) == ""
    assert M.count_fields(tree) == 3
    assert M.stats_text(tree) == "3 maydon" and M.stats_text(tree, 2) == "3 maydon · 2 o'zgarish"
    i18n.set_language("en")
    assert M.node_meta(tree, elem) == [("12,346 items", "")]
    assert M.stats_text(infer_tree([{"a": 1}]), 1) == "1 field · 1 change"
    assert M.node_meta(tree, tree.root, record_count=1) == [("1 record", "")]
    d = designer_of([{"children": [{"children": [{"x": 1}]}]}])
    d.flatten("[].children")
    depth = d.tree.find("[].children[]._depth")
    assert M.type_chips(depth) == [("123", "number", "number")] and M.sample_display(depth) == "1, 2, 3 …"


def test_known_keys() -> None:
    d = designer_of([{"a": 1, "b": {"c": 1, "d": 2}, "l": [{"x": 1}]}])
    d.delete("[].b.d")
    d.add_group("[]", "g")
    kk = M.known_keys(d.tree)
    assert kk[(WILD,)] == {"a", "b", "l"}
    assert kk[(WILD, "b")] == {"c", "d"}  # deleted on purpose: still known
    assert kk[(WILD, "l", WILD)] == {"x"}
    tree = tree_of([{"a": 1}])
    tree.known_paths = None
    assert M.known_keys(tree) == {(WILD,): frozenset({"a"})}


# ----------------------------------------------------------------------------------
# mapping files
# ----------------------------------------------------------------------------------
def test_mapping_file_round_trip(tmp_path: Path) -> None:
    d = designer_of(SAMPLE)
    d.flatten("[].children")
    d.add_group("[]", "holat")
    d.move("[].statusComment", "[].holat", "inside")
    path = tmp_path / "m.json"
    info = {"source": tmp_path / "x.json", "kind": "array", "record_path": None, "sample": 10000, "full_scan": False,
            "records": 2, "created": "2026-10-18T15:00:00+05:00", "app": "0.1.0"}
    M.save_mapping(d.tree, path, input_info=info, orig=d.orig)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == 1 and doc["input"]["source"] == str(tmp_path / "x.json") and doc["root_kind"] == "records"
    root = doc["tree"]
    assert root["src"] == [] and root["is_root"] is True and "is_elem" not in root
    elem = root["children"][0]
    assert elem["src"] == [None] and elem["is_elem"] is True
    depth = elem["children"][-2]["children"][0]["children"][0]
    assert depth["computed"] == "depth" and depth["ctx"] == [None, "children", None] and depth["src"] is None
    assert "designer" in doc and doc["known_paths"] == []  # every known path is in one of the trees
    assert list(tmp_path.iterdir()) == [path]  # atomic: no temporary file left
    tree, back_info = M.load_mapping(path)
    assert back_info == {**info, "source": str(tmp_path / "x.json")}
    assert M.node_to_dict(tree.root) == M.node_to_dict(d.tree.root)
    assert [n.id for n in tree.iter_nodes()] == list(range(1, tree.node_count() + 1))
    assert tree.known_paths == d.tree.known_paths and tree.root_kind == "records"
    assert M.node_to_dict(tree.orig.root) == M.node_to_dict(d.orig.root)
    assert min(n.id for n in tree.orig.iter_nodes()) > max(n.id for n in tree.iter_nodes())
    count = tree.node_count()
    assert M.load_mapping(path.read_bytes())[0].node_count() == count
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert M.load_mapping(str(path))[0].node_count() == count
    d2 = Designer(tree, population=lambda: iter(SAMPLE), undo_limit=300, **NOLIMIT)
    assert d2.reset().applied  # "Asl holat" after reopening
    assert M.node_to_dict(d2.tree.root) == M.node_to_dict(d.orig.root)
    # an unchanged tree stores no designer section
    t0 = tree_of([{"a": 1}])
    assert "designer" not in json.loads(M.dumps_mapping(t0, input_info={}, orig=t0))


def test_mapping_file_unicode_and_special_keys() -> None:
    recs = [{"шаҳар": "Тошкент", "ko'cha": 1, "[]": {"\u0002": 2}, "a.b": 3, "": 4, "k\ud800": 5}]
    tree = tree_of(recs)
    data = M.dumps_mapping(tree, input_info={"kind": "jsonl"})
    assert "шаҳар".encode() in data
    back, info = M.load_mapping(data)
    assert info == {"kind": "jsonl"} and back.root_kind == "records"
    assert [c.key for c in back.find("[]").children] == ["шаҳар", "ko'cha", "[]", "a.b", "", "k\ud800"]
    assert back.find([0, "[]", "\u0002"]).src == (WILD, "[]", "\u0002")


def test_root_kind_in_files() -> None:
    tree = tree_of([{"a": 1}], root_kind="keyed")
    back, _ = M.load_mapping(M.dumps_mapping(tree))
    assert back.root_kind == "keyed"
    doc = json.loads(M.dumps_mapping(tree))
    del doc["root_kind"]
    doc["input"] = {"kind": "keyed"}
    assert M.load_mapping(json.dumps(doc).encode())[0].root_kind == "keyed"
    doc["input"] = {"kind": "single"}
    assert M.load_mapping(json.dumps(doc).encode())[0].root_kind == "value"
    doc["input"] = {}
    assert M.load_mapping(json.dumps(doc).encode())[0].root_kind == "value"  # an object root
    wt = infer_tree([{"id": 1}], root_kind="wrapped", wrapper={"d": [{"x": []}]}, record_path=["d", 0, "x"])
    wdoc = json.loads(M.dumps_mapping(wt, input_info={"kind": "wrapped", "record_path": ["d", 0, "x"]}))
    back, _ = M.load_mapping(json.dumps(wdoc).encode())
    assert back.root_kind == "wrapped" and back.record_src == ("d", WILD, "x")


def _valid_doc() -> dict:
    d = designer_of([{"a": 1, "o": {"x": 1}, "l": [{"c": [{"y": 1}]}]}])
    d.flatten("[].l")
    d.add_group("[]", "g")
    return json.loads(M.dumps_mapping(d.tree, input_info={"kind": "array"}))


def _set(doc: dict, path: list, value: Any) -> dict:
    cur: Any = doc
    for p in path[:-1]:
        cur = cur[p]
    if value is _DEL:
        del cur[path[-1]]
    else:
        cur[path[-1]] = value
    return doc


_DEL = object()
_E = ["tree", "children", 0]  # the record element node
_A = _E + ["children", 0]  # field "a"
_L = _E + ["children", 2]  # array "l"


@pytest.mark.parametrize("path, value, key, pointer", [
    (["tree"], [], "mapping.error.node", "/tree"),
    (_A, 5, "mapping.error.node", "/tree/children/0/children/0"),
    (_A + ["kind"], "list", "mapping.error.kind", "/tree/children/0/children/0"),
    (_A + ["key"], 5, "mapping.error.field", "/tree/children/0/children/0"),
    (_A + ["open"], "yes", "mapping.error.field", "/tree/children/0/children/0"),
    (_A + ["types"], ["text"], "mapping.error.field", "/tree/children/0/children/0"),
    (_A + ["count"], -1, "mapping.error.field", "/tree/children/0/children/0"),
    (_A + ["count"], True, "mapping.error.field", "/tree/children/0/children/0"),
    (_A + ["pcount"], 1.5, "mapping.error.field", "/tree/children/0/children/0"),
    (_A + ["samples"], [1], "mapping.error.field", "/tree/children/0/children/0"),
    (_A + ["children"], {}, "mapping.error.field", "/tree/children/0/children/0"),
    (_A + ["rec_key"], 1, "mapping.error.field", "/tree/children/0/children/0"),
    (_A + ["src"], "a", "mapping.error.src", "/tree/children/0/children/0"),
    (_A + ["src"], [None, 1], "mapping.error.src", "/tree/children/0/children/0"),
    (_A + ["src"], [], "mapping.error.field_src", "/tree/children/0/children/0"),
    (_A + ["src"], [None, None], "mapping.error.field_src", "/tree/children/0/children/0"),
    (_A + ["is_root"], True, "mapping.error.root", "/tree/children/0/children/0"),
    (["tree", "is_root"], False, "mapping.error.root", "/tree"),
    (["tree", "src"], [None], "mapping.error.root_src", "/tree"),
    (_E + ["src"], ["x"], "mapping.error.elem_src", "/tree/children/0"),
    (_E + ["is_elem"], False, "mapping.error.array_children", "/tree/children/0"),
    (_A + ["is_elem"], True, "mapping.error.elem_outside_array", "/tree/children/0/children/0"),
    (_L + ["children"], [], "mapping.error.array_children", "/tree/children/0/children/2"),
    (_A + ["children"], [{"key": "z", "src": [None, "a", "z"]}], "mapping.error.value_children",
     "/tree/children/0/children/0"),
    (_A + ["synthetic"], True, "mapping.error.synthetic", "/tree/children/0/children/0"),
    (_E + ["children", 3, "src"], [None, "g"], "mapping.error.synthetic_src", "/tree/children/0/children/3"),
    (_L + ["children", 0, "children", 0, "ctx"], None, "mapping.error.computed",
     "/tree/children/0/children/2/children/0/children/0"),
    (_L + ["children", 0, "children", 0, "computed"], "sum", "mapping.error.computed",
     "/tree/children/0/children/2/children/0/children/0"),
    (_L + ["children", 0, "children", 0, "src"], [None, "_depth"], "mapping.error.synthetic_src",
     "/tree/children/0/children/2/children/0/children/0"),
    (_A + ["ctx"], [None], "mapping.error.ctx", "/tree/children/0/children/0"),
    (_A + ["rec_key"], "x", "mapping.error.rec_key", "/tree/children/0/children/0"),
    (["tree", "kind"], "object", "mapping.error.elem_outside_array", "/tree/children/0"),
    (["known_paths"], {}, "mapping.error.known_paths", "/known_paths"),
    (["known_paths", 1], "a", "mapping.error.known_paths", "/known_paths/1"),
    (["known_paths", 1], None, "mapping.error.known_paths", "/known_paths/1"),
    (["designer"], [], "mapping.error.designer", "/designer"),
    (["designer"], {"orig": {"kind": "array", "src": [], "children": [{"key": "a", "src": ["a"]}]}},
     "mapping.error.array_children", "/designer/orig/children/0"),
])
def test_validation_errors_name_the_bad_node(path: list, value: Any, key: str, pointer: str, lang_uz) -> None:
    doc = _set(_valid_doc(), path, value)
    with pytest.raises(MappingError) as ei:
        M.load_mapping(json.dumps(doc).encode())
    assert ei.value.key == key, ei.value
    assert ei.value.params.get("pointer") == pointer
    assert pointer in str(ei.value) and isinstance(ei.value, UserInputError)


@pytest.mark.parametrize("data, key", [
    (b"{not json", "mapping.error.json"),
    (b"\xff\xfe", "mapping.error.json"),
    (b"[1, 2]", "mapping.error.not_object"),
    (b'{"version": 2, "tree": {}}', "mapping.error.version"),
    (b'{"version": true, "tree": {}}', "mapping.error.version"),
    (b'{"tree": {}}', "mapping.error.version"),
    (b'{"version": 1, "input": [], "tree": {}}', "mapping.error.input"),
    (b'{"version": 1, "input": {}}', "mapping.error.no_tree"),
    (b'{"version": 1, "root_kind": "table", "tree": {"kind": "array", "children": [{"is_elem": true, "src": [null]}]}}',
     "mapping.error.root_kind_name"),
    (b'{"version": 1, "root_kind": "records", "tree": {"kind": "object"}}', "mapping.error.root_kind"),
    (b'{"version": 1, "input": {"kind": "wrapped", "record_path": "data"}, "tree": {"kind": "object"}}',
     "mapping.error.record_path"),
])
def test_file_level_errors(data: bytes, key: str, lang_en) -> None:
    with pytest.raises(MappingError) as ei:
        M.load_mapping(data)
    assert ei.value.key == key
    assert str(ei.value) and "mapping.error" not in str(ei.value)


def test_file_errors_from_the_file_system(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lang_uz) -> None:
    with pytest.raises(UserInputError) as ei:
        M.load_mapping(tmp_path / "missing.json")
    assert ei.value.key == "error.file_not_found"
    big = tmp_path / "big.json"
    big.write_bytes(b" " * 2048)
    monkeypatch.setattr(M, "MAX_FILE_BYTES", 1024)
    with pytest.raises(MappingError) as ei:
        M.load_mapping(big)
    assert ei.value.key == "mapping.error.file_too_big" and ei.value.hint
    with pytest.raises(Exception):
        M.save_mapping(tree_of([{"a": 1}]), tmp_path / "no" / "such" / "dir" / "m.json", input_info={})


def test_designer_orig_loaded_from_file_is_the_reset_target() -> None:
    d = designer_of([{"a": 1, "b": 2}])
    d.delete("[].b")
    data = M.dumps_mapping(d.tree, input_info={"kind": "array"}, orig=d.orig)
    tree, _ = M.load_mapping(data)
    assert tree.find("[].b") is None and tree.orig.find("[].b") is not None
    d2 = Designer(tree, undo_limit=300)
    d2.reset()
    assert d2.tree.find("[].b") is not None
    assert len({n.id for n in d2.tree.iter_nodes()}) == d2.tree.node_count()


def test_wrapped_flatten_needs_the_wrapper(lang_uz) -> None:
    doc = {"data": [{"x": [{"y": 1, "x": [{"y": 2}]}]}]}
    tree = infer_tree(doc["data"], root_kind="wrapped", wrapper={"data": []}, record_path=["data"])
    d = Designer(tree, population=lambda: iter(doc["data"]), undo_limit=300)  # no wrapper / record path
    assert d.can_flatten("data[].x")
    assert d.plan_flatten("data[].x") is None
    assert d.flatten("data[].x").reason == i18n.t("mapping.why.no_data")
    d.wrapper, d.record_path = {"data": []}, ("data",)
    assert d.flatten("data[].x").applied
    with pytest.raises(ValueError):
        infer_tree([], root_kind="wrapped", wrapper={"data": []}, record_path=())


def test_keyboard_helpers(lang_uz) -> None:
    d = designer_of([{"a": 1, "b": 2, "o": {"x": 1}, "l": [{"y": 1}], "m": [{"z": 1}]}])
    assert d.paste_zone("[].o", "[].a") == "inside"  # allowed inside: inside
    assert d.paste_zone("[].b", "[].a") == "after"  # a value row: after
    assert d.paste_zone("[].l", "[].m") == "inside"  # array onto array: merge
    assert d.paste_zone("[].l", "[].a") == "after"
    assert d.paste_zone("[]", "[].a") == "inside" and d.paste_zone("", "[].a") is None
    assert d.paste_zone("[].o.x", "[].o") is None  # into its own descendant
    r = d.move_down("[].a")
    assert r.applied and [c.key for c in d.tree.find("[]").children] == ["b", "a", "o", "l", "m"]
    r = d.move_up("[].a")
    assert r.applied and [c.key for c in d.tree.find("[]").children] == ["a", "b", "o", "l", "m"]
    assert d.move_up("[].a").reason == i18n.t("mapping.why.first")
    assert d.move_down("[].m").reason == i18n.t("mapping.why.last")
    assert d.move_up("").reason == i18n.t("mapping.why.root")
    assert d.move_up("[].nope").reason == i18n.t("mapping.why.missing")
    assert d.move_down("[]").reason == i18n.t("mapping.why.elem")  # element rows never move
    assert d.steps == 2
