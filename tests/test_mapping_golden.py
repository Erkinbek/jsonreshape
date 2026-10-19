"""Golden tests of ``jsonreshape.mapping`` against the reference engine (AGENT_PROMPT §14 tests 1-10,
docs/ARCHITECTURE.md §13.6).

Every file of ``tests/golden/mapping`` (generated from ``json-tuzilma.html`` by
``tests/golden/make_goldens.py`` through node) is replayed with :class:`jsonreshape.mapping.Designer`:

* the tree inferred like the page (from the case's ``sample`` or data, in JavaScript key order, the
  limits off) must be the reference tree node for node after the op script -- every field but the
  ids (keys, kinds, ``src``, flags, child order, ``types``, ``count``, ``pcount``, ``samples``,
  ``open``);
* every op must give the reference's ``applied`` / ``changed`` / undo depth / Uzbek toast / result
  (zones, ``canFlatten``, the new group's REF, intermediate outputs);
* the badges of every node must be the reference's (Uzbek texts, order);
* the final tree evaluated with the reference semantics (``tests/golden/reference.py``) must give the
  reference export, preview and per-record outputs (numbers compared as doubles, objects in JavaScript
  key order on both sides -- the documented deviations).

With node on PATH the goldens are also regenerated from ``json-tuzilma.html`` and must equal the
committed files (so the port matches the live reference, not only a snapshot of it).
"""

from __future__ import annotations

from typing import Any

import pytest

from golden import make_goldens
from golden import reference as R
from jsonreshape import mapping as M

GOLDENS = make_goldens.load_goldens()
_NONE = object()


def case_inputs(case: dict[str, Any]) -> tuple[str, Any, Any]:
    """(root kind, data, design data) of a harness case, in JavaScript key order."""
    if "records" in case:
        kind, data = "records", case["records"]
    else:
        kind, data = "value", case["data"]
    design = case["sample"] if "sample" in case else data
    return kind, R.js_order(data), R.js_order(design)


def make_designer(case: dict[str, Any], *, undo_limit: int = 100) -> tuple[M.Designer, Any, Any]:
    kind, data, design = case_inputs(case)
    values = design if kind == "records" else [design]
    tree = M.infer_tree(values, root_kind=kind, max_keys=None, max_depth=None, max_nodes=None)
    designer = M.Designer(tree, population=lambda: iter(values), undo_limit=undo_limit, max_keys=None,
                          max_depth=None, max_nodes=None)
    return designer, data, design


def replay(d: M.Designer, ops: list[dict[str, Any]], design: Any) -> list[dict[str, Any]]:
    """Apply harness ops (§15.3) through the Designer; log entries like the harness."""
    log = []
    for i, op in enumerate(ops):
        o = op["op"]
        result: Any = _NONE
        r: M.OpResult | None = None
        find = d.tree.find
        if o == "drop":
            n, t = find(op["node"]), find(op["target"])
            assert n is not None and t is not None, op
            z = d.zone_for(t, n, op["frac"])
            result = {"zone": z}
            if not n.is_root and not n.is_elem and z:
                r = d.move(n, t, z)
        elif o == "zone":
            n, t = find(op["node"]), find(op["target"])
            assert n is not None and t is not None, op
            result = d.zone_for(t, n, op["frac"])
        elif o == "move":
            r = d.move(find(op["node"]), find(op["target"]), op["zone"])
        elif o == "merge":
            r = d.merge(find(op["node"]), find(op["target"]))
        elif o == "rename":
            r = d.rename(find(op["node"]), op["name"])
        elif o == "delete":
            r = d.delete(find(op["node"]))
        elif o == "group":
            r = d.add_group(find(op["node"]), op.get("name"))
            if r.applied:
                result = {"ref": M.ref_of(d.tree, r.node), "key": r.node.key}
        elif o == "unwrap":
            r = d.unwrap(find(op["node"]))
        elif o == "flatten":
            r = d.flatten(find(op["node"]))
        elif o == "undo":
            r = d.undo()
        elif o == "reset":
            r = d.reset()
        elif o == "canFlatten":
            result = d.can_flatten(find(op["node"]))
        elif o == "output":
            result = R.transform(R.from_mapping_tree(d.tree), design)
        else:  # pragma: no cover - unknown op in a golden file
            raise AssertionError(o)
        applied = r is not None and r.applied
        entry: dict[str, Any] = {"i": i, "op": o, "applied": applied, "changed": bool(r is not None and r.changed),
                                 "undo": d.steps, "msg": r.message if applied else None}
        if result is not _NONE:
            entry["result"] = result
        log.append(entry)
    return log


def our_badges(tree: M.MappingTree) -> list[dict[str, Any]]:
    out = []
    for n, p, _d in tree.walk():
        texts = [b[0] for b in M.badges(tree, n, p)]
        if texts:
            out.append({"ref": M.ref_of(tree, n), "badges": texts})
    return out


def compare_logs(ours: list[dict[str, Any]], ref: list[dict[str, Any]]) -> None:
    assert len(ours) == len(ref)
    for a, b in zip(ours, ref):
        if b["op"] == "output":
            assert R.canon(a["result"]) == R.canon(b["result"]), (a["i"], "output")
            a, b = {k: v for k, v in a.items() if k != "result"}, {k: v for k, v in b.items() if k != "result"}
        assert a == b, (a, b)


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_golden_case(name: str, lang_uz) -> None:
    g = GOLDENS[name]
    case = g["case"]
    d, data, design = make_designer(case)
    log = replay(d, case.get("ops", []), design)
    compare_logs(log, g["log"])
    assert make_goldens.canon_tree(R.from_mapping_tree(d.tree)) == g["tree"]
    assert our_badges(d.tree) == g["badges"]
    ref_tree = R.from_mapping_tree(d.tree)
    assert R.canon(R.transform(ref_tree, data, case.get("limit"))) == R.canon(g["output"])
    assert [R.canon(v) for v in R.preview_values(ref_tree, data)] == [R.canon(v) for v in g["preview"]]
    if "perRecord" in g:
        per = [R.transform(ref_tree, data[i:i + 1]) for i in range(len(data))]
        assert [R.canon(v) for v in per] == [R.canon(v) for v in g["perRecord"]]


def test_golden_set_covers_the_acceptance_tests() -> None:
    names = set(GOLDENS)
    for prefix in ("t01_", "t02_", "t03_", "t04_", "t05_", "t06_", "t07_", "t08_", "t09_"):
        assert any(n.startswith(prefix) for n in names), prefix
    for ex in ("ex01", "ex02", "ex03", "ex04", "ex05", "ex06", "ex07", "ex08", "ex09", "ex10", "ex11", "ex12",
               "ex13", "ex14", "ex15", "ex16", "ex18", "ex19", "ex20", "ex21"):
        assert any(n.startswith(ex + "_") for n in names), ex
    assert len(names) == len(make_goldens.all_cases())


def test_acceptance_outputs_spelled_out() -> None:
    """The concrete statements of AGENT_PROMPT §14 tests 2, 3, 5, 6, 7, 8 on the reference outputs."""
    out = GOLDENS["t02_flatten_children"]["output"][0]["children"]
    assert [c["name"] for c in out] == ["child1", "child1.2", "child1.2.1", "child1.2.3", "child2"]
    assert [c["_depth"] for c in out] == [1, 2, 3, 3, 1]
    assert all("children" not in c for c in out)
    out = GOLDENS["t03_move_city_before_name"]["output"]
    assert list(out[0]) == ["city", "name", "contact"] and "city" not in out[0]["contact"]["address"]
    out = GOLDENS["t05_gather_oid"]["output"]
    assert out[0]["oid"] == [7, 8] and "oid" not in out[1]
    out = GOLDENS["t06_broadcast_name"]["output"]
    assert all(o["name"] == "A" for o in out[0]["orders"])
    out = GOLDENS["t07_group_holat"]["output"]
    assert out[1]["holat"] == {"statusComment": "tekshirilgan"} and out[2]["holat"] == {}
    out = GOLDENS["t08_merge_children"]["output"][0]["children"]
    assert [c["n"] for c in out] == ["c1", "c2", "c1.1", "c1.2"]


@pytest.mark.skipif(make_goldens.find_node() is None, reason="node is not on PATH")
def test_goldens_match_the_live_reference_engine() -> None:
    """Regenerate every golden from json-tuzilma.html: identical to the committed files."""
    fresh = make_goldens.build_goldens()
    assert sorted(fresh) == sorted(GOLDENS)
    stale = [name for name, g in fresh.items()
             if make_goldens.dumps_golden(g) != (make_goldens.OUT_DIR / f"{name}.json").read_text(encoding="utf-8")]
    assert not stale, f"golden files differ from the reference engine: {stale}"


@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_golden_tree_helper_builds_the_reference_tree(name: str) -> None:
    """``make_goldens.golden_tree`` (for the engine tests) gives the golden tree as a MappingTree."""
    g = GOLDENS[name]
    tree = make_goldens.golden_tree(g)
    assert make_goldens.canon_tree(R.from_mapping_tree(tree)) == g["tree"]
    kind, data, _design = make_goldens.golden_inputs(g)
    assert tree.root_kind == kind
    assert R.canon(R.transform(R.from_mapping_tree(tree), data, g["case"].get("limit"))) == R.canon(g["output"])
