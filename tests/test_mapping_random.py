"""Differential tests of ``jsonreshape.mapping`` (docs/ARCHITECTURE.md §13.6).

Random records and random edit scripts (every designer operation, drag & drop at the zone
boundaries, undo/reset, queries) are replayed with :class:`jsonreshape.mapping.Designer` and with
an independent oracle:

* ``tests/golden/reference.py`` -- the line-by-line transliteration of the json-tuzilma.html engine
  (verified against the engine on 4 000 random cases) -- with hypothesis;
* the real engine itself through node (``tests/golden/engine_harness.js``) on seeded random cases,
  when node is on PATH.

Per op: accepted or not, tree changed or not, undo depth, the Uzbek toast and the query results;
at the end: the whole tree (ids included) and the output of the final tree.
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from golden import make_goldens
from golden import reference as R
from jsonreshape import mapping as M

from test_mapping_golden import compare_logs, make_designer, replay

KEYS = ["a", "b", "c", "id", "name", "children", "items", "1", "10", "x_2", "_depth", "ko'cha", "шаҳар", "[]", ""]
NAMES = KEYS + [" z ", "yangi_guruh", "holat", "a_2", "  a  ", chr(0x3000) + "a" + chr(0x3000), chr(0x85) + "a"]
FRACS = [0.0, 0.1, 0.27, 0.28, 0.4999, 0.5, 0.72, 0.7201, 0.9, 1.0]


def _js_safe_scalars() -> st.SearchStrategy:
    return st.one_of(st.none(), st.booleans(), st.integers(-10**6, 10**6), st.sampled_from([0.5, 2.5, -3.25]),
                     st.text(alphabet="abcxyz'ш \"", max_size=4))


def _kids(key: str) -> st.SearchStrategy:
    leaf = st.fixed_dictionaries({"name": st.sampled_from(["n1", "n2", "n3"])},
                                 optional={"id": st.integers(0, 99), key: st.just([])})
    return st.recursive(st.lists(leaf, max_size=3),
                        lambda inner: st.lists(st.fixed_dictionaries({"name": st.sampled_from(["n1", "n2"])},
                                                                     optional={key: inner, "id": st.integers(0, 9)}),
                                               max_size=3), max_leaves=6)


VALUES = st.recursive(_js_safe_scalars(),
                      lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(st.sampled_from(KEYS), inner,
                                                                                            max_size=4)),
                      max_leaves=10)
RECORD = st.one_of(
    st.dictionaries(st.sampled_from(KEYS), st.one_of(VALUES, _kids("children"), _kids("items")), max_size=5),
    VALUES)
RECORDS = st.lists(RECORD, min_size=0, max_size=4).map(R.js_order)


def _all_refs(ref: R.Designer) -> list[list]:
    ref.compile_all()
    return [ref.ref_of(n) for n, _p, _d in R.walk(ref.tree)]


def _random_op(rng: random.Random, ref: R.Designer) -> dict[str, Any]:
    refs = _all_refs(ref)
    n, t = rng.choice(refs), rng.choice(refs)
    r = rng.random()
    if r < 0.25:
        return {"op": "drop", "node": n, "target": t, "frac": rng.choice(FRACS)}
    if r < 0.33:
        return {"op": "move", "node": n, "target": t, "zone": rng.choice(["before", "inside", "after"])}
    if r < 0.40:
        return {"op": "zone", "node": n, "target": t, "frac": rng.choice(FRACS)}
    if r < 0.48:
        return {"op": "rename", "node": n, "name": rng.choice(NAMES)}
    if r < 0.55:
        return {"op": "delete", "node": n}
    if r < 0.62:
        op: dict[str, Any] = {"op": "group", "node": n}
        if rng.random() < 0.7:
            op["name"] = rng.choice(["g", "holat", "a", "yangi_guruh"])
        return op
    if r < 0.69:
        return {"op": "unwrap", "node": n}
    if r < 0.80:
        flat = [x for x in refs if ref.can_flatten(ref.resolve_ref(x))]
        return {"op": "flatten", "node": rng.choice(flat) if flat else n}
    if r < 0.88:
        arrays = [x for x in refs if ref.resolve_ref(x)["kind"] == "array"]
        movable = [x for x in arrays if x and not ref.resolve_ref(x)["isElem"]]
        return {"op": "merge", "node": rng.choice(movable) if movable else n, "target": rng.choice(arrays or refs)}
    if r < 0.92:
        return {"op": "canFlatten", "node": n}
    return {"op": rng.choice(["undo", "undo", "reset"])}


def _run_both(records: list[Any], seed: int, n_ops: int) -> None:
    rng = random.Random(seed)
    ref = R.Designer(records)
    tree = M.infer_tree(records, max_keys=None, max_depth=None, max_nodes=None)
    ours = M.Designer(tree, population=lambda: iter(records), undo_limit=100, max_keys=None, max_depth=None,
                      max_nodes=None)
    for i in range(n_ops):
        op = _random_op(rng, ref)
        expected = ref.apply(op)
        got = replay(ours, [op], records)[0]
        got.pop("i")
        if op["op"] == "output":  # pragma: no cover - not generated
            continue
        assert got == expected, (i, op, got, expected)
        assert json.dumps(R.from_mapping_tree(ours.tree), ensure_ascii=False) == json.dumps(ref.tree, ensure_ascii=False)
    assert R.canon(R.transform(R.from_mapping_tree(ours.tree), records)) == R.canon(R.transform(ref.tree, records))
    for n, p, _d in ours.tree.walk():  # badges agree with the oracle's rowHTML logic
        assert [b[0] for b in M.badges(ours.tree, n, p)] == next(
            (b["badges"] for b in R.badges_of(ref.tree) if b["ref"] == M.ref_of(ours.tree, n)), [])


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large,
                                                      HealthCheck.function_scoped_fixture])
@given(records=RECORDS, seed=st.integers(0, 2**32 - 1), n_ops=st.integers(0, 12))
def test_designer_equals_the_reference_transliteration(records: list[Any], seed: int, n_ops: int, lang_uz) -> None:
    _run_both(records, seed, n_ops)


@pytest.mark.parametrize("seed", range(40))
def test_designer_equals_the_reference_on_recursive_data(seed: int, lang_uz) -> None:
    rng = random.Random(seed)
    records = R.js_order(_fuzz_records(rng))
    _run_both(records, seed, 14)


# ----------------------------------------------------------------------------------
# the real engine (node)
# ----------------------------------------------------------------------------------
def _fuzz_val(rng: random.Random, d: int) -> Any:
    r = rng.random()
    if d > 3 or r < 0.35:
        return rng.choice([0, 1, 7, -3, 2.5, "p", "q", "", None, True, False, "o'zgarish"])
    if r < 0.7:
        return _fuzz_obj(rng, d + 1)
    return [(_fuzz_obj(rng, d + 1) if rng.random() < 0.85 else _fuzz_val(rng, d + 2)) for _ in range(rng.randrange(4))]


def _fuzz_obj(rng: random.Random, d: int) -> dict:
    o: dict = {}
    for _ in range(1 + rng.randrange(4)):
        k = rng.choice(KEYS)
        o[k] = _fuzz_kids(rng, d, k) if k in ("children", "items") and rng.random() < 0.8 else _fuzz_val(rng, d)
    return o


def _fuzz_kids(rng: random.Random, d: int, k: str) -> list:
    if d > 4:
        return []
    out = []
    for _ in range(rng.randrange(3)):
        o: dict = {"name": rng.choice(["n1", "n2", "n3"])}
        if rng.random() < 0.7:
            o[k] = _fuzz_kids(rng, d + 1, k)
        if rng.random() < 0.5:
            o["id"] = rng.randrange(100)
        out.append(o)
    return out


def _fuzz_records(rng: random.Random) -> list:
    return [(_fuzz_obj(rng, 0) if rng.random() < 0.95 else _fuzz_val(rng, 2)) for _ in range(1 + rng.randrange(4))]


@pytest.mark.skipif(make_goldens.find_node() is None, reason="node is not on PATH")
def test_designer_equals_the_live_engine_on_random_cases(lang_uz) -> None:
    rng = random.Random(20261018)
    cases = []
    for ci in range(300):
        records = R.js_order(_fuzz_records(rng))
        ref = R.Designer(records)
        ops = []
        for _ in range(rng.randrange(10)):
            op = _random_op(rng, ref)
            ref.apply(op)
            ops.append(op)
        cases.append({"name": f"random{ci}", "records": records, "ops": ops})
    results = make_goldens.run_harness(cases)
    for case, res in zip(cases, results):
        d, data, design = make_designer(case)
        log = replay(d, case["ops"], design)
        compare_logs(log, res["log"])
        assert make_goldens.canon_tree(R.from_mapping_tree(d.tree)) == make_goldens.canon_tree(res["tree"]), case["name"]
        assert [n.id for n in d.tree.iter_nodes()] == [n["id"] for n, _p, _d in R.walk(res["tree"])], case["name"]
        assert R.canon(R.transform(R.from_mapping_tree(d.tree), data)) == R.canon(res["output"]), case["name"]
