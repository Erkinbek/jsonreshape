#!/usr/bin/env python
"""Regenerate the mapping golden files (``tests/golden/mapping/*.json``) from the reference engine.

The reference is the engine of the project-root ``json-tuzilma.html``. ``engine_harness.js`` (this
folder; plain node, no npm packages) loads it unchanged into ``node:vm``, infers the tree like the
page, replays an edit script and prints what the prototype exports (docs/ARCHITECTURE.md §13.6,
AGENT_PROMPT §14 test 10)::

    python tests/golden/make_goldens.py            # rewrite every golden file
    python tests/golden/make_goldens.py --check    # compare with the committed files (exit 1 on a difference)
    python tests/golden/make_goldens.py --only t03_move_city_before_name ex09_merge_into_root

Cases: AGENT_PROMPT §14 tests 1-9 (``t01_*`` … ``t09_*``, including the drag & drop zone matrices with
the exact 0.28 / 0.5 / 0.72 boundaries), the worked examples of the engine analysis (``ex*``; Ex 17 is
left out: it only shows JavaScript number artefacts), the undo-limit case and the targeted cases of
``cases_extra.json`` (``x_*``). Every golden file holds:

* ``case``: the harness input -- ``records`` (an array / JSON Lines file) or ``data`` (any root value),
  optional ``sample`` (the data the tree is inferred from and ``flatten`` reads), ``ops`` (the harness op
  script: move / drop / zone / merge / rename / delete / group / unwrap / flatten / undo / reset /
  canFlatten / output), optional ``limit``;
* ``output``: the reference export of the final tree (``transform(DATA)``);
* ``log``: per op ``i``, ``op``, ``applied``, ``changed``, ``undo`` (undo depth), ``msg`` (the Uzbek toast;
  ``null`` when refused) and ``result`` (zone / canFlatten / group ref / output);
* ``tree``: the final tree in canonical form (no ids; ``src``/``ctx`` as segment lists with ``null`` for
  the array step; snake_case flags; children last);
* ``badges`` (Uzbek badge texts of every node that has some), ``shape`` (readable outline), ``preview``
  (the live preview of every record: limit 21, 1-element unwrap) and, for record files, ``perRecord``.

The inputs avoid the JavaScript artefacts the port deliberately does not reproduce (numbers beyond
doubles, ``1e400``). Integer-like keys are fine: the Python test feeds every input in JavaScript key
order. Generated files are deterministic; regenerate when ``json-tuzilma.html`` changes.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
HARNESS = HERE / "engine_harness.js"
OUT_DIR = HERE / "mapping"
EXTRA_CASES = HERE / "cases_extra.json"
REPO = HERE.parents[1]
HTML = REPO / "json-tuzilma.html"

SEP, JS_WILD = "\u0001", "\u0002"
HARNESS_FLAGS = ("--compact", "--badges", "--shape", "--per-record", "--preview")

# ----------------------------------------------------------------------------------------------
# case data
# ----------------------------------------------------------------------------------------------
# the prototype's built-in sample (json-tuzilma.html lines 546-560)
SAMPLE: list[Any] = [
    {"id": 1, "name": "parent1", "statusComment": None,
     "contact": {"phone": "+998 90 123 45 67", "address": {"city": "Toshkent", "street": "Navoiy 12"}},
     "children": [
         {"id": 11, "name": "child1", "statusComment": None, "children": [
             {"id": 112, "name": "child1.2", "children": [
                 {"id": 1121, "name": "child1.2.1", "children": []},
                 {"id": 1123, "name": "child1.2.3", "statusComment": "ok", "children": []}]}]},
         {"id": 12, "name": "child2", "children": []}]},
    {"id": 2, "name": "parent2", "statusComment": "tekshirilgan",
     "contact": {"phone": "+998 93 555 44 33", "address": {"city": "Samarqand"}},
     "children": [
         {"id": 21, "name": "child2.1", "children": [
             {"id": 211, "name": "child2.1.1", "children": []}]}]},
]

CHILDREN = [{"name": "p1", "children": [
    {"name": "child1", "children": [
        {"name": "child1.2", "children": [{"name": "child1.2.1", "children": []},
                                         {"name": "child1.2.3", "children": []}]}]},
    {"name": "child2", "children": []}]}]

CONTACTS = [{"name": "p1", "contact": {"phone": "1", "address": {"city": "Toshkent", "street": "Navoiy"}}},
            {"name": "p2", "contact": {"phone": "2", "address": {"city": "Samarqand"}}}]

ORDERS = [{"name": "A", "orders": [{"oid": 7, "sum": 10}, {"oid": 8, "sum": 20}]}, {"name": "B"},
          {"name": "C", "orders": []}, {"name": "D", "orders": [{"sum": 5}, {"oid": None}]},
          {"name": "E", "orders": None}, {"name": "F", "orders": "x"}]

# one record with every kind of row, for the drag & drop matrices
ZONE_RECORDS = [{"id": 1, "name": "a", "contact": {"phone": "1", "address": {"city": "T", "zip": "100"}},
                 "tags": ["x", "y"], "children": [{"n": "c", "children": [{"n": "d"}]}],
                 "grid": [[1, 2], [3]], "empty": []}]
ZONE_TARGETS = ["", "[]", "[].id", "[].contact", "[].contact.address", "[].contact.address.city", "[].tags",
                "[].tags[]", "[].children", "[].children[]", "[].children[].children", "[].children[].children[]",
                "[].grid", "[].grid[]", "[].empty", "[].empty[]", "[].g"]
ZONE_FRACS = [0.0, 0.27, 0.2799, 0.28, 0.4999, 0.5, 0.72, 0.7201, 0.73, 1.0]
ZONE_DRAGGED = {"value": "[].id", "nested_value": "[].contact.address.city", "object": "[].contact",
                "scalar_array": "[].tags", "object_array": "[].children[].children", "group": "[].g",
                "record_elem": "[]", "root": ""}


def _c(name: str, description: str, **kw: Any) -> dict[str, Any]:
    return {"name": name, "description": description, **kw}


def _mv(node: Any, target: Any, zone: str) -> dict[str, Any]:
    return {"op": "move", "node": node, "target": target, "zone": zone}


def _drop(node: Any, target: Any, frac: float) -> dict[str, Any]:
    return {"op": "drop", "node": node, "target": target, "frac": frac}


def agent_prompt_cases() -> list[dict[str, Any]]:
    """AGENT_PROMPT §14 tests 1-9."""
    out: list[dict[str, Any]] = []
    t1 = "AGENT_PROMPT §14 test 1 (identity): "
    out += [
        _c("t01_identity_records", t1 + "array root whose records share one key order: output == input",
           records=[{"id": 1, "name": "a", "tags": ["x"], "contact": {"city": "T", "zip": None}},
                    {"id": 2, "name": "b", "tags": [], "contact": {"city": "S", "zip": "100"}}]),
        _c("t01_identity_sample", t1 + "the prototype SAMPLE (child1.2.3 is reordered to the tree's order)",
           records=SAMPLE),
        _c("t01_identity_object_root", t1 + "object root", data={"meta": {"v": 1}, "data": [{"id": 1}, {"id": 2, "x": None}]}),
        _c("t01_identity_keyed_tree", t1 + "keyed tree child1 / child1.2", data={"child1": {"child1.2": {"id": 112}}, "child2": {"id": 2}}),
        _c("t01_identity_mixed_types", t1 + "a field of several types; objects and arrays pass through",
           records=[{"f": {"x": 1}}, {"f": "str"}, {"g": [1, 2]}, {"g": {"k": 1}}, {"g": 7}]),
        _c("t01_identity_special_keys", t1 + "__proto__, constructor and the empty key are ordinary keys",
           records=[{"__proto__": {"x": 1}, "": 2, "constructor": 3}]),
        _c("t01_identity_unicode", t1 + "Uzbek Latin / Cyrillic keys and values, apostrophes, escapes",
           records=[{"shahar": "Toshkent", "ko'cha": "Navoiy 12", "izoh": "o'zgarish «yangi»"},
                    {"шаҳар": "Тошкент", "ko'cha": "Amir Temur", "izoh": "satr\nikkinchi \"qator\" \t tab"}]),
    ]
    for i, v in enumerate([5, "s", None, True, [], {}, [[1, 2], [3]], [1, "x", {"a": 1}, [2]]]):
        out.append(_c(f"t01_identity_value_{i}", t1 + "single value " + json.dumps(v), data=v))

    t2 = "AGENT_PROMPT §14 test 2 (recursive flatten): "
    out += [
        _c("t02_flatten_children", t2 + "child1, child1.2, child1.2.1, child1.2.3, child2 with _depth 1,2,3,3,1; "
           "no children inside", records=CHILDREN,
           ops=[{"op": "canFlatten", "node": "[].children"}, {"op": "flatten", "node": "[].children"},
                {"op": "canFlatten", "node": "[].children"}]),
        _c("t02_flatten_sample", t2 + "the prototype SAMPLE", records=SAMPLE,
           ops=[{"op": "flatten", "node": "[].children"}]),
        _c("t02_flatten_root", t2 + "the root array: records and all descendants", records=SAMPLE,
           ops=[{"op": "canFlatten", "node": ""}, {"op": "flatten", "node": ""}]),
        _c("t02_flatten_edits_lost", t2 + "renames / deletes under the element are re-inferred away", records=SAMPLE,
           ops=[{"op": "rename", "node": "[].children[].name", "name": "nom"},
                {"op": "delete", "node": "[].children[].id"}, {"op": "flatten", "node": "[].children"}]),
        _c("t02_flatten_foreign_kept", t2 + "a moved-in node survives; a data _depth makes the computed _depth_2",
           records=[{"name": "p", "children": [{"name": "c", "_depth": "x", "children": [{"name": "g"}]}]}],
           ops=[_mv("[].name", "[].children[]", "inside"), {"op": "flatten", "node": "[].children"}]),
        _c("t02_flatten_scalars", t2 + "scalar elements are kept, children:null is not descended",
           records=[{"children": [{"id": 1, "children": [{"id": 2}, 5, {"id": 3, "children": None}]}, "str"]}],
           ops=[{"op": "flatten", "node": "[].children"}]),
        _c("t02_flatten_same_name_preferred", t2 + "two candidate arrays: the one named like the array wins",
           records=[{"items": [{"sub": [{"a": 1}], "items": [{"b": 2, "items": [{"b": 3}]}]}]}],
           ops=[{"op": "flatten", "node": "[].items"}]),
        _c("t02_flatten_root_first_candidate", t2 + "at the root the first object array of the record is chosen",
           records=[{"id": 1, "orders": [{"oid": 7}], "children": [{"id": 2}]}],
           ops=[{"op": "flatten", "node": ""}]),
        _c("t02_flatten_undo", t2 + "flatten is one undo step", records=CHILDREN,
           ops=[{"op": "flatten", "node": "[].children"}, {"op": "output"}, {"op": "undo"}, {"op": "output"}]),
    ]

    t3 = "AGENT_PROMPT §14 test 3 (move): "
    out += [
        _c("t03_move_city_before_name", t3 + "contact.address.city before name, gone from address", records=CONTACTS,
           ops=[_mv("[].contact.address.city", "[].name", "before")]),
        _c("t03_move_city_sample_drop", t3 + "the same as a mouse drop on the top of the name row (SAMPLE)",
           records=SAMPLE, ops=[_drop("[].contact.address.city", "[].name", 0.1)]),
        _c("t03_move_after_and_inside", t3 + "after / inside / back to its own parent (appended)", records=CONTACTS,
           ops=[_mv("[].name", "[].contact.address", "inside"), {"op": "output"},
                _mv("[].contact.address.name", "[].contact", "after"), {"op": "output"},
                _mv("[].name", "[]", "inside")]),
    ]

    t4 = "AGENT_PROMPT §14 test 4 (unwrap): "
    out += [
        _c("t04_unwrap_contact", t4 + "phone and address move one level up", records=CONTACTS,
           ops=[{"op": "unwrap", "node": "[].contact"}]),
        _c("t04_unwrap_contact_sample", t4 + "SAMPLE", records=SAMPLE, ops=[{"op": "unwrap", "node": "[].contact"}]),
        _c("t04_unwrap_collision", t4 + "a child takes a free name with a number",
           records=[{"city": "A", "address": {"city": "B", "zip": 1}}], ops=[{"op": "unwrap", "node": "[].address"}]),
        _c("t04_unwrap_group", t4 + "a new group can be dissolved again", records=[{"a": 1, "b": 2}],
           ops=[{"op": "group", "node": "[]", "name": "g"}, _mv("[].a", "[].g", "inside"),
                {"op": "unwrap", "node": "[].g"}]),
    ]

    t5 = "AGENT_PROMPT §14 test 5 (gather): "
    out += [
        _c("t05_gather_oid", t5 + "orders[].oid at record level: [7, 8]; no field without orders", records=ORDERS,
           ops=[_mv("[].orders[].oid", "[].name", "after")]),
        _c("t05_gather_nested", t5 + "two-level gather and a gathered array node",
           records=[{"orders": [{"items": [{"sku": "x", "qty": 2}, {"sku": "y"}]}, {"items": []}]}, {"orders": []}, {}],
           ops=[_mv("[].orders[].items[].sku", "[].orders", "before"), _mv("[].orders[].items", "[].orders", "before")]),
    ]

    t6 = "AGENT_PROMPT §14 test 6 (broadcast): "
    out += [
        _c("t06_broadcast_name", t6 + "name inside orders[]: every order gets the name",
           records=[{"name": "A", "orders": [{"oid": 7}, {"oid": 8}]}], ops=[_mv("[].name", "[].orders[]", "inside")]),
        _c("t06_broadcast_drop", t6 + "as a mouse drop, records with missing / empty / null orders",
           records=[{"name": "Ali", "orders": [{"oid": 7, "sum": 1}, {"oid": 8}]}, {"name": "Vali"},
                    {"name": "G", "orders": []}, {"name": "N", "orders": None},
                    {"name": "X", "orders": [{"sum": 2}, {"oid": None}]}],
           ops=[_drop("[].name", "[].orders[]", 0.5)]),
    ]

    t7 = "AGENT_PROMPT §14 test 7 (group): "
    out += [
        _c("t07_group_holat", t7 + "new group, statusComment moved in, the group renamed to holat",
           records=SAMPLE + [{"id": 3, "name": "parent3"}],
           ops=[{"op": "group", "node": "[]"}, _mv("[].statusComment", "[].yangi_guruh", "inside"),
                {"op": "rename", "node": "[].yangi_guruh", "name": "holat"}]),
        _c("t07_group_named", t7 + "group named at once (two undo steps), always written",
           records=[{"id": 1, "statusComment": "ok"}, {"id": 2}],
           ops=[{"op": "group", "node": "[]", "name": "holat"}, _mv("[].statusComment", "[].holat", "inside")]),
        _c("t07_group_names", t7 + "unique default names, nested groups, renames", records=[{"a": 1, "c": {"x": 1}}],
           ops=[{"op": "group", "node": "[]", "name": "holat"}, {"op": "group", "node": "[]"},
                {"op": "group", "node": "[]"}, {"op": "group", "node": "[].holat"},
                {"op": "group", "node": "[].c"}, {"op": "rename", "node": "[].yangi_guruh_2", "name": "holat"},
                {"op": "group", "node": "[].a"}]),
    ]

    t8 = "AGENT_PROMPT §14 test 8 (merge): "
    out += [
        _c("t08_merge_children", t8 + "children[].children dropped onto children: elements concatenated",
           records=[{"children": [{"n": "c1", "children": [{"n": "c1.1"}, {"n": "c1.2"}]}, {"n": "c2"}]}],
           ops=[{"op": "merge", "node": "[].children[].children", "target": "[].children"}]),
        _c("t08_merge_drop_delete", t8 + "mouse drop (SAMPLE), then the merged element source is removed",
           records=SAMPLE,
           ops=[{"op": "zone", "node": "[].children[].children", "target": "[].children", "frac": 0.5},
                _drop("[].children[].children", "[].children", 0.5), {"op": "output"},
                {"op": "delete", "node": "[].children[1]"}, {"op": "delete", "node": "[].children[0]"}]),
        _c("t08_merge_into_root", t8 + "into the root array: all records first, then the merged items",
           records=[{"n": 1, "k": [{"m": "a"}]}, {"n": 2, "k": [{"m": "b"}]}],
           ops=[{"op": "merge", "node": "[].k", "target": ""}]),
        _c("t08_merge_scalar_arrays", t8 + "arrays of scalars", data={"a": [1, 2], "b": [3]},
           ops=[{"op": "merge", "node": "b", "target": "a"}]),
    ]

    t9 = "AGENT_PROMPT §14 test 9 (drag & drop rules): "
    for label, dragged in ZONE_DRAGGED.items():
        ops: list[dict[str, Any]] = [{"op": "group", "node": "[]", "name": "g"}]
        for target in ZONE_TARGETS:
            for frac in ZONE_FRACS:
                ops.append({"op": "zone", "node": dragged, "target": target, "frac": frac})
        out.append(_c(f"t09_zones_{label}", t9 + f"zoneFor matrix of {dragged!r} over every row kind and the "
                      "0.28 / 0.5 / 0.72 boundaries", records=ZONE_RECORDS, ops=ops))
    depth_ops: list[dict[str, Any]] = [{"op": "flatten", "node": "[].children"}]
    for frac in ZONE_FRACS:
        for dragged, target in (("[].children[]._depth", "[].name"), ("[].children[]._depth", "[].children[]"),
                                ("[].name", "[].children[]._depth"), ("[].children[]._depth", "[].children")):
            depth_ops.append({"op": "zone", "node": dragged, "target": target, "frac": frac})
    out.append(_c("t09_zones_computed", t9 + "the computed _depth row", records=CHILDREN, ops=depth_ops))
    out += [
        _c("t09_rule_28_percent", t9 + "top 28 % before, middle inside, bottom 28 % after", records=ZONE_RECORDS,
           ops=[_drop("[].name", "[].contact", 0.1), {"op": "output"}, {"op": "undo"},
                _drop("[].name", "[].contact", 0.5), {"op": "output"}, {"op": "undo"},
                _drop("[].name", "[].contact", 0.9), {"op": "output"}, {"op": "undo"}]),
        _c("t09_rule_no_inside_value", t9 + "nothing can be dropped inside a value row", records=ZONE_RECORDS,
           ops=[_drop("[].contact", "[].id", 0.5), {"op": "undo"}, _mv("[].contact", "[].id", "inside"),
                _mv("[].name", "[].tags[]", "inside"), _drop("[].name", "[].tags[]", 0.5)]),
        _c("t09_rule_not_into_descendant", t9 + "a node cannot go onto itself or its descendants",
           records=ZONE_RECORDS,
           ops=[_drop("[].contact", "[].contact.address", 0.5), _mv("[].contact", "[].contact.address", "inside"),
                _mv("[].contact", "[].contact.address.city", "before"), _mv("[].contact", "[].contact", "after"),
                _drop("[].contact", "[].contact", 0.5)]),
        _c("t09_rule_arrays_onto_arrays", t9 + "only an array goes inside an array (merge)", records=ZONE_RECORDS,
           ops=[_drop("[].id", "[].tags", 0.5), {"op": "undo"}, _mv("[].id", "[].tags", "inside"),
                _mv("[].contact", "[].children", "inside"), _drop("[].children[].children", "[].children", 0.5),
                _drop("[].grid", "[].tags", 0.5)]),
        _c("t09_rule_root_and_elem_rows", t9 + "root and element rows are never dragged; no siblings for them",
           records=ZONE_RECORDS,
           ops=[_drop("", "[].id", 0.5), _drop("[]", "[].contact", 0.5), _drop("[].children[]", "[].tags", 0.5),
                _mv("", "[].contact", "inside"), _mv("[]", "[].contact", "inside"),
                _mv("[].children[]", "[].contact", "inside"), _mv("[].id", "[]", "before"),
                _mv("[].id", "", "after"), _drop("[].id", "", 0.5), _drop("[].tags", "", 0.5)]),
    ]
    return out


def example_cases() -> list[dict[str, Any]]:
    """The worked examples of the engine analysis (engine_map §7; Ex 17 left out)."""
    ex = "engine analysis worked example: "
    return [
        _c("ex01_identity_tree_order", ex + "identity normalizes key order to the tree",
           records=[{"id": 1, "name": "a"}, {"name": "b", "id": 2, "extra": True}]),
        _c("ex02_flatten_t2", ex + "recursive flatten", records=CHILDREN, ops=[{"op": "flatten", "node": "[].children"}]),
        _c("ex03_move_t3", ex + "move city before name", records=CONTACTS,
           ops=[_mv("[].contact.address.city", "[].name", "before")]),
        _c("ex04_unwrap_t4", ex + "unwrap contact", records=CONTACTS, ops=[{"op": "unwrap", "node": "[].contact"}]),
        _c("ex05_gather_t5", ex + "gather orders[].oid", records=ORDERS, ops=[_mv("[].orders[].oid", "[].name", "after")]),
        _c("ex06_broadcast_t6", ex + "broadcast name into orders[]",
           records=[{"name": "A", "orders": [{"oid": 7}, {"oid": 8}]}], ops=[_mv("[].name", "[].orders[]", "inside")]),
        _c("ex07_group_t7", ex + "group + rename + move", records=[{"id": 1, "statusComment": "ok"}, {"id": 2}],
           ops=[{"op": "group", "node": "[]", "name": "holat"}, _mv("[].statusComment", "[].holat", "inside")]),
        _c("ex08_merge_t8", ex + "merge children[].children onto children",
           records=[{"children": [{"n": "c1", "children": [{"n": "c1.1"}, {"n": "c1.2"}]}, {"n": "c2"}]}],
           ops=[{"op": "merge", "node": "[].children[].children", "target": "[].children"}]),
        _c("ex09_merge_into_root", ex + "merge into the root array",
           records=[{"n": 1, "k": [{"m": "a"}]}, {"n": 2, "k": [{"m": "b"}]}],
           ops=[{"op": "merge", "node": "[].k", "target": ""}]),
        _c("ex10_collisions", ex + "key collisions _2, _3 on the whole key",
           records=[{"name": "r", "name_2": "x", "a_2": 1, "kids": [{"name": "k1", "a_2": 2}, {"name": "k2"}]}],
           ops=[_mv("[].kids[].name", "[]", "inside"), _mv("[].kids[].a_2", "[].a_2", "after"),
                {"op": "rename", "node": "[].kids", "name": "name"}]),
        _c("ex11_type_mismatch", ex + "type mismatch passes through; fields outside the tree are dropped",
           sample=[{"o": {"a": 1}, "arr": [{"x": 1}], "v": "s"}],
           records=[{"o": "str", "arr": {"x": 9}, "v": {"deep": [1]}},
                    {"o": {"a": 2, "zz": 1}, "arr": ["p", {"x": 2, "y": 3}], "v": None, "extra": 1}]),
        _c("ex12_array_vs_value_gather", ex + "array-node gather vs value-node gather",
           records=[{"k": [{"c": [1, 2], "v": 1}, {"c": [3], "v": 2}]}, {"k": [{"c": "s1"}, {"c": "s2"}]},
                    {"k": [{"c": "s1"}, {"c": [4]}]}, {"k": []}],
           ops=[_mv("[].k[].c", "[].k", "before"), _mv("[].k[].v", "[].k", "before")]),
        _c("ex13_nested_broadcast", ex + "deepest bound prefix (nested broadcast)",
           records=[{"name": "P", "kids": [{"name": "K1", "toys": [{"t": 1}, {"t": 2}]}, {"name": "K2", "toys": [{"t": 3}]}]}],
           ops=[_mv("[].kids[].name", "[].kids[].toys[]", "inside"), _mv("[].name", "[].kids[].toys[]", "inside")]),
        _c("ex14_flatten_foreign", ex + "flatten re-infers; foreign nodes, _depth_2, duplicate keys",
           records=[{"x": "outer", "kids": [{"n": 1, "_depth": "d", "kids": [{"n": 2, "x": "inner"}, "prim"]}]}],
           ops=[{"op": "rename", "node": "[].kids[].n", "name": "N"}, _mv("[].x", "[].kids[]", "inside"),
                {"op": "flatten", "node": "[].kids"}]),
        _c("ex15_depth_moves", ex + "_depth inside a group, then outside the element", records=CHILDREN,
           ops=[{"op": "flatten", "node": "[].children"}, {"op": "group", "node": "[].children[]", "name": "meta"},
                _mv("[].children[]._depth", "[].children[].meta", "inside"), {"op": "output"},
                _mv("[].children[].meta._depth", "[].name", "after")]),
        _c("ex16_key_order", ex + "array-index keys (JavaScript order on both sides)",
           records=[{"b": 1, "10": 2, "a": 3, "2": 4}], ops=[_mv("[].10", "[].a", "after")]),
        _c("ex18_flatten_root", ex + "flatten the root array",
           records=[{"n": "r1", "children": [{"n": "c1", "children": [{"n": "g1"}]}]}, {"n": "r2"}],
           ops=[{"op": "flatten", "node": ""}]),
        _c("ex19_gathered_object", ex + "gathered object node",
           records=[{"orders": [{"oid": 7, "customer": {"n": "x", "t": 1}}, {"oid": 8, "customer": "raw"}, {"oid": 9}]}],
           ops=[_mv("[].orders[].customer", "[].orders", "before")]),
        _c("ex20_array_wins_over_object", ex + "array beats object in inference", records=[{"f": {"a": 1}}, {"f": [1]}]),
        _c("ex21_object_root", ex + "object root gather and broadcast",
           data={"list": [{"a": 1}, {"a": 2}], "meta": {"v": 1}},
           ops=[_mv("list[].a", "list", "before"), _mv("meta.v", "list[]", "inside")]),
    ]


def undo_cases() -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = [{"op": "rename", "node": [0, 0], "name": f"k{i}"} for i in range(105)]
    ops += [{"op": "undo"} for _ in range(101)]
    return [
        _c("x_undo_cap", "the reference keeps 100 undo steps (the Python test replays it with undo_limit=100)",
           records=[{"a": 1}], ops=ops),
        _c("x_undo_reset_sequence", "undo / reset (reset is undoable and always a step)",
           records=[{"a": 1, "b": {"c": 2}}],
           ops=[{"op": "undo"}, {"op": "reset"}, {"op": "rename", "node": "[].a", "name": "A"},
                {"op": "unwrap", "node": "[].b"}, {"op": "output"}, {"op": "reset"}, {"op": "output"},
                {"op": "undo"}, {"op": "output"}, {"op": "undo"}, {"op": "undo"}, {"op": "undo"}, {"op": "undo"},
                {"op": "reset"}, {"op": "reset"}]),
    ]


def extra_cases() -> list[dict[str, Any]]:
    return json.loads(EXTRA_CASES.read_text(encoding="utf-8"))


def all_cases() -> list[dict[str, Any]]:
    """Every golden case definition (fresh copies)."""
    cases = agent_prompt_cases() + example_cases() + undo_cases() + extra_cases()
    names = [c["name"] for c in cases]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate golden case names: {sorted(dupes)}")
    return copy.deepcopy(cases)


# ----------------------------------------------------------------------------------------------
# running the reference
# ----------------------------------------------------------------------------------------------
def find_node() -> str | None:
    """The node executable, or ``None``."""
    return shutil.which("node")


def harness_input(case: dict[str, Any]) -> dict[str, Any]:
    """The case as the harness reads it (description dropped)."""
    return {k: v for k, v in case.items() if k != "description"}


def run_harness(cases: list[dict[str, Any]], *, node: str | None = None, html: Path | None = None,
                timeout: float = 600) -> list[dict[str, Any]]:
    """Run the reference engine on ``cases`` (one node process) and return its result objects."""
    node = node or find_node()
    if node is None:
        raise RuntimeError("node is not on PATH")
    cmd = [node, str(HARNESS), "-", *HARNESS_FLAGS, "--html", str(html or HTML)]
    payload = json.dumps([harness_input(c) for c in cases], ensure_ascii=False)
    proc = subprocess.run(cmd, input=payload.encode("utf-8"), capture_output=True, timeout=timeout, check=False)
    text = proc.stdout.decode("utf-8")
    if proc.returncode not in (0, 1) or not text.strip():
        raise RuntimeError(f"harness failed ({proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[:2000]}")
    results = json.loads(text)
    for case, res in zip(cases, results):
        if "error" in res:
            raise RuntimeError(f"golden case {case['name']} failed in the reference: {res['error'][:2000]}")
    if len(results) != len(cases):
        raise RuntimeError("the harness returned a different number of results")
    return results


def _seg(s: Any) -> Any:
    return None if s == JS_WILD else s


def canon_tree(tree: dict[str, Any]) -> dict[str, Any]:
    """A reference snapshot tree in canonical form (no ids, null array steps, snake_case, children
    last); iterative."""
    out: dict[str, Any] | None = None
    stack: list[tuple[dict[str, Any], list | None]] = [(tree, None)]
    while stack:
        n, dest = stack.pop()
        ctx = n.get("ctx")
        if isinstance(ctx, str):
            ctx = [_seg(s) for s in ctx.split(SEP)]
        elif isinstance(ctx, list):
            ctx = [_seg(s) for s in ctx]
        src = n.get("src")
        d = {"key": n.get("key", ""), "kind": n.get("kind", "value"),
             "src": [_seg(s) for s in src] if isinstance(src, list) else None,
             "types": list(n.get("types", [])), "count": n.get("count", 0), "pcount": n.get("pcount"),
             "samples": list(n.get("samples", [])), "synthetic": bool(n.get("synthetic")),
             "rec_key": n.get("recKey"), "computed": n.get("computed"), "ctx": ctx, "open": bool(n.get("open")),
             "is_elem": bool(n.get("isElem")), "is_root": bool(n.get("isRoot")), "children": []}
        if dest is None:
            out = d
        else:
            dest.append(d)
        stack.extend((c, d["children"]) for c in reversed(n.get("children", [])))
    assert out is not None
    return out


def golden_of(case: dict[str, Any], res: dict[str, Any]) -> dict[str, Any]:
    g: dict[str, Any] = {"name": case["name"], "description": case.get("description", ""),
                         "case": harness_input(case), "output": res.get("output"), "log": res.get("log", []),
                         "tree": canon_tree(res["tree"]), "badges": res.get("badges", []),
                         "shape": res.get("shape", []), "preview": res.get("preview", [])}
    if "perRecord" in res:
        g["perRecord"] = res["perRecord"]
        g["perRecordConcatEqualsOutput"] = res.get("perRecordConcatEqualsOutput")
    return g


_SURROGATE = re.compile("[\ud800-\udfff]")
_WIDTH = 110


def _smart(v: Any, indent: int) -> str:
    """JSON with short values on one line and longer containers broken one item per line."""
    s = json.dumps(v, ensure_ascii=False)
    if len(s) + indent <= _WIDTH or not isinstance(v, (dict, list)) or not v:
        return s
    pad = " " * (indent + 1)
    if isinstance(v, list):
        items = [pad + _smart(x, indent + 1) for x in v]
        return "[\n" + ",\n".join(items) + "\n" + " " * indent + "]"
    items = [pad + json.dumps(k, ensure_ascii=False) + ": " + _smart(x, indent + 1) for k, x in v.items()]
    return "{\n" + ",\n".join(items) + "\n" + " " * indent + "}"


def _dump_tree(node: dict[str, Any], indent: int) -> str:
    """One tree node per line (its fields compact), children indented below it."""
    head = json.dumps({k: v for k, v in node.items() if k != "children"}, ensure_ascii=False)[:-1]
    kids = node["children"]
    if not kids:
        return head + ', "children": []}'
    lines = [head + ', "children": [']
    for i, c in enumerate(kids):
        lines.append(" " * (indent + 1) + _dump_tree(c, indent + 1) + ("," if i < len(kids) - 1 else ""))
    lines.append(" " * indent + "]}")
    return "\n".join(lines)


def dumps_golden(g: dict[str, Any]) -> str:
    parts = []
    for k, v in g.items():
        val = _dump_tree(v, 1) if k == "tree" else _smart(v, 1)
        parts.append(" " + json.dumps(k, ensure_ascii=False) + ": " + val)
    text = "{\n" + ",\n".join(parts) + "\n}\n"
    if _SURROGATE.search(text):
        text = _SURROGATE.sub(lambda m: "\\u%04x" % ord(m.group()), text)
    return text


def build_goldens(*, node: str | None = None, html: Path | None = None,
                  only: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """``name -> golden`` for every case (or ``only`` these), computed by the reference."""
    cases = all_cases()
    if only:
        wanted = set(only)
        cases = [c for c in cases if c["name"] in wanted]
        missing = wanted - {c["name"] for c in cases}
        if missing:
            raise ValueError(f"unknown golden case names: {sorted(missing)}")
    results = run_harness(cases, node=node, html=html)
    return {c["name"]: golden_of(c, r) for c, r in zip(cases, results)}


def load_goldens(directory: Path = OUT_DIR) -> dict[str, dict[str, Any]]:
    """The committed golden files (``name -> golden``)."""
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="compare with the committed files instead of writing")
    ap.add_argument("--only", nargs="*", help="only these case names")
    ap.add_argument("--node", help="node executable (default: node on PATH)")
    ap.add_argument("--html", type=Path, help="json-tuzilma.html (default: the project root's)")
    ap.add_argument("--out", type=Path, default=OUT_DIR, help="output folder")
    args = ap.parse_args(argv)
    goldens = build_goldens(node=args.node, html=args.html, only=args.only)
    if args.check:
        bad = []
        for name, g in goldens.items():
            path = args.out / f"{name}.json"
            if not path.exists() or path.read_text(encoding="utf-8") != dumps_golden(g):
                bad.append(name)
        if not args.only:
            stale = sorted(set(p.stem for p in args.out.glob("*.json")) - set(goldens))
            bad += [f"{n} (no such case)" for n in stale]
        for name in bad:
            sys.stdout.write(f"differs: {name}\n")
        sys.stdout.write(f"{len(goldens)} cases checked, {len(bad)} differ\n")
        return 1 if bad else 0
    args.out.mkdir(parents=True, exist_ok=True)
    if not args.only:
        for p in args.out.glob("*.json"):
            if p.stem not in goldens:
                p.unlink()
    for name, g in goldens.items():
        (args.out / f"{name}.json").write_text(dumps_golden(g), encoding="utf-8", newline="\n")
    sys.stdout.write(f"wrote {len(goldens)} golden files to {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ----------------------------------------------------------------------------------------------
# helpers for tests of other modules (the engine): the final tree and the inputs of a golden case
# ----------------------------------------------------------------------------------------------
def golden_inputs(g: dict[str, Any]) -> tuple[str, Any, Any]:
    """``(root kind, data, design data)`` of a golden case, in JavaScript key order (like the page):
    ``records`` cases are record files (root kind ``records``), ``data`` cases one value (``value``)."""
    from golden.reference import js_order

    case = g["case"]
    if "records" in case:
        kind, data = "records", case["records"]
    else:
        kind, data = "value", case["data"]
    design = case["sample"] if "sample" in case else data
    return kind, js_order(data), js_order(design)


def golden_tree(g: dict[str, Any]) -> Any:
    """The reference's final tree of a golden case as a ``jsonreshape.mapping.MappingTree``."""
    from jsonreshape import mapping

    kind = "records" if "records" in g["case"] else "value"
    return mapping.MappingTree(mapping.tree_from_dict(g["tree"]), root_kind=kind)
