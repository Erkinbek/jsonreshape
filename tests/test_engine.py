"""The mapping evaluation (jsonreshape.engine, docs/ARCHITECTURE.md §13.2): the plain port of the reference
evaluation, the compiled evaluator and the preview.

* every committed golden (tests/golden/mapping, generated from json-tuzilma.html) -- outputs, previews and the
  per-record transforms -- with both evaluators, compared modulo the documented artefacts (``canon``: numbers
  as doubles, objects in JavaScript key order on both sides);
* the compiled evaluator gives byte-identical results to the plain port (exact numbers, insertion order) on
  the goldens, on hypothesis-generated records and edit scripts, with and without a limit;
* the §13.3 decomposition (``items(E_j, r)`` over the root element nodes, the "no array reached" fallback);
* live differential against the reference engine through node (skipped without node);
* semantics pinned by hand: MISSING vs null, gathers, broadcasts, type mismatches, ``_depth``, exact numbers,
  Unicode, the unknown-keys flag, deep data.
"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from jsonreshape import codec, engine, mapping
from jsonreshape.engine import MISSING, CompiledMapping, PlainEvaluator, preview, transform_plain
from jsonreshape.mapping import WILD, Designer, MapNode, MappingTree, infer_tree

GOLDEN = Path(__file__).resolve().parent / "golden"
GOLDEN_FILES = sorted((GOLDEN / "mapping").glob("*.json")) if (GOLDEN / "mapping").is_dir() else []
HARNESS = GOLDEN / "engine_harness.js"
NODE = shutil.which("node")


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
_INDEX_RE = re.compile(r"0|[1-9][0-9]*")


def _is_index(k: str) -> bool:
    return _INDEX_RE.fullmatch(k) is not None and int(k) <= 4294967294


def js_keys(d: dict) -> list:
    return sorted((k for k in d if _is_index(k)), key=int) + [k for k in d if not _is_index(k)]


def canon(v: Any) -> Any:
    """Comparison form modulo the documented artefacts: objects in JS key order, numbers as doubles."""
    if isinstance(v, dict):
        return ("{}", [(k, canon(v[k])) for k in js_keys(v)])
    if isinstance(v, list):
        return ("[]", [canon(x) for x in v])
    if v is None or isinstance(v, (bool, str)):
        return v
    return ("num", float(v))


def exact(v: Any) -> bytes:
    """Byte form keeping insertion order and exact number text."""
    return codec.dumps(None if v is MISSING else v)


def compiled(tree: Any, value: Any, limit: int | None = None) -> Any:
    return CompiledMapping(tree, limit=limit).transform_value(value)


def both(tree: Any, value: Any, limit: int | None = None) -> Any:
    """The plain and the compiled result, asserted byte-identical; returns it."""
    a = transform_plain(tree, value, limit=limit)
    b = compiled(tree, value, limit=limit)
    assert exact(b) == exact(a)
    return a


def records_tree(recs: list, **kw: Any) -> MappingTree:
    return infer_tree(recs, root_kind="records", max_keys=None, max_depth=None, max_nodes=None, **kw)


def designer(recs: list) -> Designer:
    return Designer(records_tree(recs), population=lambda: iter(recs), undo_limit=500)


def node(tree: MappingTree, ref: str) -> MapNode:
    n = tree.find(ref)
    assert n is not None, ref
    return n


def decomposed(tree: Any, recs: list) -> Any:
    """§13.3: items(E_j, r) element-node-major, then the reference fallback."""
    cm = CompiledMapping(tree)
    items: list = []
    # an element node whose source array is the record array itself reaches an array even without records
    saw = any(e.is_elem and e.src is not None and len(e.src) == 1 for e in getattr(tree, "root", tree).children)
    fb: Any = MISSING
    for j in range(cm.root_elems):
        for r in recs:
            out, s, f = cm.record_items(r, elem=j)
            items.extend(out)
            saw = saw or s
            if f is not MISSING and fb is MISSING:
                fb = f
    if saw:
        return items
    return None if fb is MISSING else fb


# ----------------------------------------------------------------------------------
# goldens (generated from the reference, tests/golden/make_goldens.py)
# ----------------------------------------------------------------------------------
def _golden(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.skipif(not GOLDEN_FILES, reason="no golden files")
@pytest.mark.parametrize("path", GOLDEN_FILES, ids=[p.stem for p in GOLDEN_FILES])
def test_golden_outputs_previews_and_per_record(path: Path) -> None:
    g = _golden(path)
    case = g["case"]
    data = case["records"] if "records" in case else case["data"]
    tree = mapping.tree_from_dict(g["tree"])
    out = both(tree, data)
    assert canon(out) == canon(g["output"])
    if isinstance(data, list):
        for i, rec in enumerate(data):
            pv = preview(tree, rec, records=True)
            assert canon(pv) == canon(g["preview"][i]), i
            whole = both(tree, [rec])
            unwrapped = whole[0] if isinstance(whole, list) and len(whole) == 1 else whole
            assert exact(preview(tree, rec, records=True, limit=None)) == exact(unwrapped)
        if "perRecord" in g:
            for i, rec in enumerate(data):
                assert canon(both(tree, [rec])) == canon(g["perRecord"][i]), i
        if tree.kind == "array":
            assert exact(decomposed(tree, data)) == exact(out)
    else:
        assert canon(preview(tree, data, records=False)) == canon(g["preview"][0])


@pytest.mark.skipif(not GOLDEN_FILES, reason="no golden files")
def test_golden_set_covers_the_acceptance_cases() -> None:
    names = {p.stem for p in GOLDEN_FILES}
    for prefix in ("t01_", "t02_", "t03_", "t04_", "t05_", "t06_", "t07_", "t08_", "t09_", "ex"):
        assert any(n.startswith(prefix) for n in names), prefix


# ----------------------------------------------------------------------------------
# the reference semantics, pinned by hand (AGENT_PROMPT §14 tests 1-8, engine_map §7)
# ----------------------------------------------------------------------------------
SAMPLE = [
    {"id": 1, "name": "parent1", "statusComment": None,
     "contact": {"phone": "+998 90 123 45 67", "address": {"city": "Toshkent", "street": "Navoiy 12"}},
     "children": [
         {"id": 11, "name": "child1", "children": [
             {"id": 112, "name": "child1.2", "children": [
                 {"id": 1121, "name": "child1.2.1", "children": []},
                 {"id": 1123, "name": "child1.2.3", "statusComment": "ok", "children": []}]}]},
         {"id": 12, "name": "child2", "statusComment": "yo'q", "children": []}]},
    {"id": 2, "name": "parent2", "statusComment": "faol",
     "contact": {"phone": "+998 91 000 00 00", "address": {"city": "Samarqand"}}, "children": []},
]


def test_identity_normalizes_key_order_to_the_tree() -> None:
    recs = [{"id": 1, "name": "a"}, {"name": "b", "id": 2, "extra": True}]
    d = designer(recs)
    assert both(d.tree, recs) == [{"id": 1, "name": "a"}, {"id": 2, "name": "b", "extra": True}]
    assert list(both(d.tree, recs)[1]) == ["id", "name", "extra"]


def test_identity_keeps_every_value_in_the_tree_key_order() -> None:
    d = designer(SAMPLE)
    out = both(d.tree, SAMPLE)
    assert json.dumps(out, sort_keys=True) == json.dumps(SAMPLE, sort_keys=True)
    leaf = out[0]["children"][0]["children"][0]["children"][1]
    assert leaf["name"] == "child1.2.3"
    assert list(leaf) == ["id", "name", "children", "statusComment"]    # engine_map §4: first-seen order


def test_recursive_flatten_children_preorder_and_depth() -> None:
    recs = [{"name": "p1", "children": [
        {"name": "child1", "children": [{"name": "child1.2", "children": [
            {"name": "child1.2.1", "children": []}, {"name": "child1.2.3", "children": []}]}]},
        {"name": "child2", "children": []}]}]
    d = designer(recs)
    assert d.flatten(node(d.tree, "[].children")).applied
    out = both(d.tree, recs)
    kids = out[0]["children"]
    assert [k["name"] for k in kids] == ["child1", "child1.2", "child1.2.1", "child1.2.3", "child2"]
    assert [k["_depth"] for k in kids] == [1, 2, 3, 3, 1]
    assert all("children" not in k for k in kids)
    assert list(kids[0]) == ["_depth", "name"]


def test_move_city_before_name_and_unwrap_contact() -> None:
    recs = [{"name": "p1", "contact": {"phone": "1", "address": {"city": "Toshkent", "street": "Navoiy"}}},
            {"name": "p2", "contact": {"phone": "2", "address": {"city": "Samarqand"}}}]
    d = designer(recs)
    assert d.move(node(d.tree, "[].contact.address.city"), node(d.tree, "[].name"), "before").applied
    assert both(d.tree, recs) == [
        {"city": "Toshkent", "name": "p1", "contact": {"phone": "1", "address": {"street": "Navoiy"}}},
        {"city": "Samarqand", "name": "p2", "contact": {"phone": "2", "address": {}}}]
    d2 = designer(recs)
    assert d2.unwrap(node(d2.tree, "[].contact")).applied
    assert both(d2.tree, recs) == [
        {"name": "p1", "phone": "1", "address": {"city": "Toshkent", "street": "Navoiy"}},
        {"name": "p2", "phone": "2", "address": {"city": "Samarqand"}}]


def test_gather_missing_null_and_non_array_containers() -> None:
    recs = [{"name": "A", "orders": [{"oid": 7, "sum": 10}, {"oid": 8, "sum": 20}]}, {"name": "B"},
            {"name": "C", "orders": []}, {"name": "D", "orders": [{"sum": 5}, {"oid": None}]},
            {"name": "E", "orders": None}, {"name": "F", "orders": "x"}]
    d = designer(recs)
    assert d.move(node(d.tree, "[].orders[].oid"), node(d.tree, "[].name"), "after").applied
    out = both(d.tree, recs)
    assert out == [
        {"name": "A", "oid": [7, 8], "orders": [{"sum": 10}, {"sum": 20}]},
        {"name": "B"},
        {"name": "C", "oid": [], "orders": []},
        {"name": "D", "oid": [None], "orders": [{"sum": 5}, {}]},
        {"name": "E", "oid": [], "orders": None},
        {"name": "F", "oid": [], "orders": "x"}]
    assert "oid" not in out[1]                       # MISSING: not written; null is written


def test_broadcast_and_nested_broadcast_deepest_bound_prefix() -> None:
    recs = [{"name": "A", "orders": [{"oid": 7}, {"oid": 8}]}]
    d = designer(recs)
    assert d.move(node(d.tree, "[].name"), node(d.tree, "[].orders[]"), "inside").applied
    assert both(d.tree, recs) == [{"orders": [{"oid": 7, "name": "A"}, {"oid": 8, "name": "A"}]}]
    recs2 = [{"name": "P", "kids": [{"name": "K1", "toys": [{"t": 1}, {"t": 2}]}, {"name": "K2", "toys": [{"t": 3}]}]}]
    d2 = designer(recs2)
    assert d2.move(node(d2.tree, "[].kids[].name"), node(d2.tree, "[].kids[].toys[]"), "inside").applied
    assert d2.move(node(d2.tree, "[].name"), node(d2.tree, "[].kids[].toys[]"), "inside").applied
    assert both(d2.tree, recs2) == [{"kids": [
        {"toys": [{"t": 1, "name": "K1", "name_2": "P"}, {"t": 2, "name": "K1", "name_2": "P"}]},
        {"toys": [{"t": 3, "name": "K2", "name_2": "P"}]}]}]


def test_group_is_always_written_and_merge_concatenates() -> None:
    recs = [{"id": 1, "statusComment": "ok"}, {"id": 2}]
    d = designer(recs)
    assert d.add_group(node(d.tree, "[]"), "holat").applied
    assert d.move(node(d.tree, "[].statusComment"), node(d.tree, "[].holat"), "inside").applied
    assert both(d.tree, recs) == [{"id": 1, "holat": {"statusComment": "ok"}}, {"id": 2, "holat": {}}]
    recs2 = [{"children": [{"n": "c1", "children": [{"n": "c1.1"}, {"n": "c1.2"}]}, {"n": "c2"}]}]
    d2 = designer(recs2)
    assert d2.move(node(d2.tree, "[].children[].children"), node(d2.tree, "[].children"), "inside").applied
    assert both(d2.tree, recs2) == [{"children": [{"n": "c1"}, {"n": "c2"}, {"n": "c1.1"}, {"n": "c1.2"}]}]


def test_merge_into_root_is_element_node_major_and_decomposes() -> None:
    recs = [{"n": 1, "k": [{"m": "a"}]}, {"n": 2, "k": [{"m": "b"}]}]
    d = designer(recs)
    assert d.move(node(d.tree, "[].k"), node(d.tree, ""), "inside").applied
    out = both(d.tree, recs)
    assert out == [{"n": 1}, {"n": 2}, {"m": "a"}, {"m": "b"}]
    cm = CompiledMapping(d.tree)
    assert cm.root_elems == 2
    assert cm.transform_record(recs[0], elem=0) == [{"n": 1}]
    assert cm.transform_record(recs[0], elem=1) == [{"m": "a"}]
    assert decomposed(d.tree, recs) == out
    # per-record transforms concatenated are NOT the export (the reason for one pass per element node)
    assert [x for r in recs for x in both(d.tree, [r])] != out


@pytest.mark.parametrize("recs, expected", [
    ([{"k": "x"}, {"k": [1]}], [1]),
    ([{"k": "x"}, {"k": "y"}], "x"),
    ([{"a": 1}], None),
])
def test_root_with_only_a_merged_element_node_falls_back(recs: list, expected: Any) -> None:
    sample = [{"r": 1, "k": [1]}]
    d = designer(sample)
    assert d.move(node(d.tree, "[].k"), node(d.tree, ""), "inside").applied
    assert d.delete(node(d.tree, "[]")).applied
    assert both(d.tree, recs) == expected
    assert decomposed(d.tree, recs) == expected


def test_array_node_gather_vs_value_node_gather() -> None:
    recs = [{"k": [{"c": [1, 2], "v": 1}, {"c": [3], "v": 2}]}, {"k": [{"c": "s1"}, {"c": "s2"}]},
            {"k": [{"c": "s1"}, {"c": [4]}]}, {"k": []}]
    d = designer(recs)
    assert d.move(node(d.tree, "[].k[].c"), node(d.tree, "[].k"), "before").applied
    assert d.move(node(d.tree, "[].k[].v"), node(d.tree, "[].k"), "before").applied
    assert both(d.tree, recs) == [{"c": [1, 2, 3], "v": [1, 2], "k": [{}, {}]}, {"c": "s1", "v": [], "k": [{}, {}]},
                                  {"c": [4], "v": [], "k": [{}, {}]}, {"v": [], "k": []}]


def test_type_mismatch_passes_through_and_unknown_fields_are_dropped() -> None:
    sample = [{"o": {"a": 1}, "arr": [{"x": 1}], "v": "s"}]
    tree = records_tree(sample)
    recs = [{"o": "str", "arr": {"x": 9}, "v": {"deep": [1]}},
            {"o": {"a": 2, "zz": 1}, "arr": ["p", {"x": 2, "y": 3}], "v": None, "extra": 1}]
    assert both(tree, recs) == [{"o": "str", "arr": {"x": 9}, "v": {"deep": [1]}},
                                {"o": {"a": 2}, "arr": ["p", {"x": 2}], "v": None}]


def test_unknown_keys_flag_counts_only_fields_the_mapping_does_not_know() -> None:
    sample = [{"a": 1, "b": {"c": 1}, "l": [{"x": 1}]}]
    d = designer(sample)
    assert d.delete(node(d.tree, "[].a")).applied          # deleted on purpose: still known
    cm = CompiledMapping(d.tree, check_unknown=True)
    cm.transform_value([{"a": 5, "b": {"c": 2}, "l": [{"x": 2}]}])
    assert not cm.unknown_seen
    for rec in ({"zz": 1}, {"b": {"c": 1, "new": 2}}, {"l": [{"x": 1, "y": 2}]}):
        cm.transform_value([rec])
        assert cm.unknown_seen, rec
    cm.transform_value([{"b": "raw", "l": "raw"}])     # raw pass-through loses nothing
    assert not cm.unknown_seen


def test_depth_is_missing_outside_the_flattened_element() -> None:
    recs = [{"name": "p1", "children": [{"name": "c1", "children": [{"name": "g1"}]}]}]
    d = designer(recs)
    assert d.flatten(node(d.tree, "[].children")).applied
    assert d.add_group(node(d.tree, "[].children[]"), "meta").applied
    assert d.move(node(d.tree, "[].children[]._depth"), node(d.tree, "[].children[].meta"), "inside").applied
    assert both(d.tree, recs)[0]["children"] == [{"name": "c1", "meta": {"_depth": 1}},
                                                 {"name": "g1", "meta": {"_depth": 2}}]
    assert d.move(node(d.tree, "[].children[].meta._depth"), node(d.tree, "[].name"), "after").applied
    out = both(d.tree, recs)
    assert "_depth" not in out[0] and out[0]["children"][0] == {"name": "c1", "meta": {}}


def test_root_flatten_records_and_descendants() -> None:
    recs = [{"n": "r1", "children": [{"n": "c1", "children": [{"n": "g1"}]}]}, {"n": "r2"}]
    d = designer(recs)
    assert d.flatten(node(d.tree, "")).applied
    expected = [{"_depth": 1, "n": "r1"}, {"_depth": 2, "n": "c1"}, {"_depth": 3, "n": "g1"}, {"_depth": 1, "n": "r2"}]
    assert both(d.tree, recs) == expected
    assert decomposed(d.tree, recs) == expected


def test_object_root_gather_and_broadcast() -> None:
    data = {"list": [{"a": 1}, {"a": 2}], "meta": {"v": 1}}
    tree = infer_tree([data], root_kind="value")
    d = Designer(tree, population=lambda: iter([data]), undo_limit=500)
    assert d.move(node(d.tree, "list[].a"), node(d.tree, "list"), "before").applied
    assert d.move(node(d.tree, "meta.v"), node(d.tree, "list[]"), "inside").applied
    assert both(d.tree, data) == {"a": [1, 2], "list": [{"v": 1}, {"v": 1}], "meta": {}}


def test_wrapper_transform_places_the_records() -> None:
    wrapper = {"meta": {"v": 1}, "data": [], "tail": True}
    recs = [{"x": 1}, {"x": 2, "y": 3}]
    tree = infer_tree(recs, root_kind="wrapped", wrapper=wrapper, record_path=("data",))
    cm = CompiledMapping(tree)
    assert cm.transform_wrapper(wrapper, recs, ("data",)) == {"meta": {"v": 1}, "data": recs, "tail": True}
    assert wrapper["data"] == []                      # the caller's value is not changed
    with pytest.raises(ValueError):
        cm.transform_wrapper(wrapper, recs, ("nope",))


def test_exact_numbers_unicode_and_escapes_survive() -> None:
    text = ('[{"n":[0.1,1e400,12345678901234567890,-0.0,1.10,-0,1E5],"ism":"Oʻzbekiston Тошкент 😀",'
            '"esc":"a\\"b\\\\c\\u0001","kalit bo\'shliq":{"10":1,"2":2}}]')
    recs = codec.loads(text)
    d = designer(recs)
    out = both(d.tree, recs)
    assert codec.dumps(out) == codec.dumps(recs)
    assert b"1e400" in codec.dumps(out) and b"12345678901234567890" in codec.dumps(out)
    assert b"-0.0" in codec.dumps(out) and b"1.10" in codec.dumps(out)
    assert list(out[0]["kalit bo'shliq"]) == ["10", "2"]          # insertion order (documented deviation)


def test_preview_limit_and_unwrap() -> None:
    recs = [{"a": list(range(30)), "o": [{"v": i} for i in range(30)]}]
    d = designer(recs)
    assert d.move(node(d.tree, "[].o[].v"), node(d.tree, "[].a"), "after").applied
    pv = preview(d.tree, recs[0])
    assert isinstance(pv, dict)                         # one-element result unwrapped
    assert pv["a"] == list(range(21)) and pv["v"] == list(range(21)) and len(pv["o"]) == 21
    assert preview(d.tree, recs[0], limit=None)["v"] == list(range(30))
    d2 = designer([{"n": 1, "k": [{"m": "a"}]}])
    assert d2.move(node(d2.tree, "[].k"), node(d2.tree, ""), "inside").applied
    assert preview(d2.tree, {"n": 1, "k": [{"m": "a"}]}) == [{"n": 1}, {"m": "a"}]   # not unwrapped
    assert engine.PREVIEW_LIMIT == 21


def test_limit_zero_means_no_limit_like_the_reference() -> None:
    recs = [{"a": list(range(50))}]
    tree = records_tree(recs)
    assert both(tree, recs, limit=0) == recs


def test_missing_root_gives_null_and_evaluate_keeps_missing() -> None:
    sample = [{"r": 1, "k": [1]}]
    d = designer(sample)
    d.move(node(d.tree, "[].k"), node(d.tree, ""), "inside")
    d.delete(node(d.tree, "[]"))
    cm = CompiledMapping(d.tree)
    assert cm.evaluate([{"a": 1}]) is MISSING
    assert cm.transform_value([{"a": 1}]) is None
    assert repr(MISSING) == "MISSING"


def test_keyed_member_value_tree() -> None:
    members = [{"id": 1, "tags": ["a"]}, {"id": 2, "name": "x"}]
    tree = infer_tree(members, root_kind="keyed")
    assert tree.root.kind == "object" and tree.root.src == ()
    cm = CompiledMapping(tree)
    assert [cm.transform_value(m) for m in members] == members
    assert cm.transform_value("raw") == "raw"           # type mismatch at the root passes through
    elem = MapNode(id=2, kind="value", src=("x", WILD), is_elem=True)
    only_x = MappingTree(MapNode(id=1, kind="array", src=(), is_root=True, children=[elem]), root_kind="keyed")
    cm2 = CompiledMapping(only_x)
    assert cm2.transform_member({"x": [1, 2]}) == [1, 2]
    assert cm2.transform_member({"y": 1}) is MISSING   # the member is not written
    assert cm2.transform_value({"y": 1}) is None


def _chain(depth: int) -> list:
    rec: Any = {"name": "leaf"}
    for i in range(depth):
        rec = {"name": f"n{i}", "children": [rec]}
    return [rec]


def test_deep_data_and_deep_trees_do_not_recurse_out() -> None:
    # the compiled evaluator: a tree 3 000 nodes deep (the plain port is cubic in the depth, like the
    # reference's binding-chain lookups, so it is cross-checked at a smaller depth)
    recs = _chain(1500)
    tree = records_tree(recs)
    assert codec.dumps(compiled(tree, recs)) == codec.dumps(recs)
    d = Designer(records_tree(recs), population=lambda: iter(recs), undo_limit=500)
    assert d.flatten(node(d.tree, "[].children")).applied
    flat = compiled(d.tree, recs)[0]["children"]
    assert len(flat) == 1500 and flat[-1] == {"_depth": 1500, "name": "leaf"}
    small = _chain(250)
    assert codec.dumps(both(records_tree(small), small)) == codec.dumps(small)


def test_preorder_is_iterative() -> None:
    root: dict = {"k": []}
    cur = root
    for i in range(20000):
        nxt = {"i": i, "k": []}
        cur["k"].append(nxt)
        cur = nxt
    items = list(engine.preorder(root["k"], "k"))
    assert len(items) == 20000 and items[-1][1] == 20000
    assert [d for _, d in engine.preorder([{"k": [1, {"k": [2]}]}, 3], "k")] == [1, 2, 2, 3, 1]


def test_compile_path_keys() -> None:
    keys, full = engine.compile_path((WILD, "children", WILD, "name"))
    assert keys == [(WILD,), None, (WILD, "children", WILD), None]
    assert full == (WILD, "children", WILD, "name")


def test_node_without_source_is_a_programming_error() -> None:
    root = MapNode(id=1, kind="array", src=(), is_root=True,
                   children=[MapNode(id=2, kind="object", src=(WILD,), is_elem=True,
                                     children=[MapNode(id=3, key="x", src=None)])])
    with pytest.raises(ValueError):
        PlainEvaluator(root, [])
    with pytest.raises(ValueError):
        CompiledMapping(root)


# ----------------------------------------------------------------------------------
# differential: plain port == compiled evaluator == the §13.3 decomposition
# ----------------------------------------------------------------------------------
KEYS = ["id", "name", "children", "kids", "orders", "oid", "a", "b", "c", "10", "2", "Тошкент", "kalit bo'shliq", ""]
NUMBERS = ["0", "-0", "0.1", "1.10", "-0.0", "1e400", "12345678901234567890", "7", "-3", "2.5"]

scalars = st.one_of(st.none(), st.booleans(), st.sampled_from(NUMBERS).map(codec.loads),
                    st.integers(-5, 5), st.sampled_from(["x", "Oʻzbek", "s\"q", "😀", ""]))
values = st.recursive(
    scalars,
    lambda inner: st.one_of(st.lists(inner, max_size=3),
                            st.dictionaries(st.sampled_from(KEYS), inner, max_size=4)),
    max_leaves=12)
records_st = st.lists(st.dictionaries(st.sampled_from(KEYS), values, max_size=5), min_size=1, max_size=5)


def _random_ops(d: Designer, rng: random.Random, n: int) -> None:
    for _ in range(n):
        nodes = list(d.tree.iter_nodes())
        a, b = rng.choice(nodes), rng.choice(nodes)
        op = rng.random()
        try:
            if op < 0.45:
                d.move(a, b, rng.choice(("before", "inside", "after")))
            elif op < 0.55:
                d.delete(a)
            elif op < 0.65:
                d.unwrap(a)
            elif op < 0.75:
                d.add_group(a, rng.choice([None, "g", "name"]))
            elif op < 0.82:
                d.rename(a, rng.choice(["name", "x", "a_2", "children"]))
            elif op < 0.95:
                if d.can_flatten(a):
                    d.flatten(a)
            else:
                d.undo()
        except ValueError:
            pass


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(recs=records_st, seed=st.integers(0, 10**6), nops=st.integers(0, 8))
def test_compiled_equals_plain_on_random_trees(recs: list, seed: int, nops: int) -> None:
    rng = random.Random(seed)
    d = designer(recs)
    _random_ops(d, rng, nops)
    tree = d.tree
    other = [{rng.choice(KEYS): v} for v in recs] + recs       # records with fields the tree may not know
    for data in (recs, other):
        for limit in (None, 3):
            want = both(tree, data, limit=limit)
            checked = CompiledMapping(tree, limit=limit, check_unknown=True)
            assert exact(checked.transform_value(data)) == exact(want)
            assert exact(checked.transform_value(data)) == exact(want)      # warm shape caches
        if tree.root.kind == "array":
            assert exact(decomposed(tree, data)) == exact(transform_plain(tree, data))
    for rec in recs:
        pv = preview(tree, rec)
        out = transform_plain(tree, [rec], limit=21)
        assert exact(pv) == exact(out[0] if isinstance(out, list) and len(out) == 1 else out)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=values, seed=st.integers(0, 10**6), nops=st.integers(0, 6))
def test_compiled_equals_plain_on_random_value_roots(data: Any, seed: int, nops: int) -> None:
    tree = infer_tree([data], root_kind="value", max_keys=None, max_depth=None, max_nodes=None)
    d = Designer(tree, population=lambda: iter([data]), undo_limit=500)
    _random_ops(d, random.Random(seed), nops)
    for limit in (None, 2):
        both(d.tree, data, limit=limit)


# ----------------------------------------------------------------------------------
# live differential against the reference engine (node + tests/golden/engine_harness.js): random records
# and random edit scripts run by the REFERENCE; its final tree is evaluated here (only the engine is tested)
# ----------------------------------------------------------------------------------
_W, _SEP = "\u0002", "\u0001"


def tree_from_reference(d: dict) -> MapNode:
    """A tree in the reference engine's snapshot format (camelCase flags, ``"\\u0002"`` for the array step,
    ``ctx`` as the ``"\\u0001"``-joined compiled path) as MapNodes (iterative)."""
    def segs(s: Any) -> tuple | None:
        if s is None:
            return None
        if isinstance(s, str):
            return tuple(None if x == _W else x for x in s.split(_SEP)) if s else ()
        return tuple(None if x == _W else x for x in s)
    root: MapNode | None = None
    stack: list[tuple[dict, list | None]] = [(d, None)]
    while stack:
        x, dest = stack.pop()
        n = MapNode(id=int(x.get("id", 0)), key=x.get("key", ""), kind=x.get("kind", "value"), src=segs(x.get("src")),
                    synthetic=bool(x.get("synthetic")), rec_key=x.get("recKey"), computed=x.get("computed"),
                    ctx=segs(x.get("ctx")), is_elem=bool(x.get("isElem")), is_root=bool(x.get("isRoot")))
        if dest is None:
            root = n
        else:
            dest.append(n)
        stack.extend((c, n.children) for c in reversed(x.get("children", [])))
    assert root is not None
    return root


def _rand_js(rng: random.Random, depth: int) -> Any:
    r = rng.random()
    if depth > 2 or r < 0.35:
        return rng.choice([None, True, False, 3, -2, 0.5, "x", "Oʻzbek", "😀", ""])
    if r < 0.65:
        return [_rand_js(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {k: _rand_js(rng, depth + 1) for k in rng.sample(["id", "name", "kids", "o", "v"], rng.randint(0, 4))}


def _random_case(rng: random.Random, i: int) -> dict:
    recs = [{k: _rand_js(rng, 0) for k in rng.sample(["id", "name", "kids", "o", "v", "x"], rng.randint(1, 5))}
            for _ in range(rng.randint(1, 4))]
    count = sum(1 for _ in records_tree(recs).iter_nodes())     # ids 1..count exist after inference
    ops = []
    for _ in range(rng.randint(0, 7)):
        a, b = {"id": rng.randint(1, count)}, {"id": rng.randint(1, count)}
        r = rng.random()
        if r < 0.5:
            ops.append({"op": "move", "node": a, "target": b, "zone": rng.choice(("before", "inside", "after"))})
        elif r < 0.6:
            ops.append({"op": "delete", "node": a})
        elif r < 0.7:
            ops.append({"op": "unwrap", "node": a})
        elif r < 0.85:
            ops.append({"op": "flatten", "node": a})
        else:
            ops.append({"op": "group", "node": a, "name": "g"})
    return {"name": f"live{i}", "records": recs, "ops": ops}


@pytest.mark.skipif(NODE is None or not HARNESS.is_file(), reason="node or the harness is not available")
def test_live_differential_against_the_reference_engine() -> None:
    rng = random.Random(20261018)
    cases = [_random_case(rng, i) for i in range(300)]
    proc = subprocess.run([NODE, str(HARNESS), "-", "--compact", "--per-record"], input=json.dumps(cases),
                          capture_output=True, text=True, encoding="utf-8", timeout=300,
                          cwd=str(GOLDEN.parent.parent))
    assert proc.returncode in (0, 1), proc.stderr
    results = json.loads(proc.stdout)
    compared = 0
    for c, r in zip(cases, results):
        if "error" in r:
            continue
        tree = tree_from_reference(r["tree"])
        out = both(tree, c["records"])
        assert canon(out) == canon(r["output"]), c
        for rec, per in zip(c["records"], r["perRecord"]):
            assert canon(both(tree, [rec])) == canon(per), c
        if tree.kind == "array":
            assert exact(decomposed(tree, c["records"])) == exact(out), c
        compared += 1
    assert compared >= 250
