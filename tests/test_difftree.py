"""jsonreshape.gui.difftree: the aligned rows of a structural diff (Qt-free; ARCHITECTURE §14.9
"Compare", s5/map_repair_diff.md §4.7).

Rows / statuses / key labels / texts for objects, arrays (auto, index, key with moves,
unordered), type changes, lone values, exact numbers (``1`` = ``1.0``, ``-0`` = ``0``, ``1e400``,
big integers, ``numbers="text"`` with character highlights), Unicode; unchanged subtrees
collapsed into one summary row; "only differences" with gap rows (expansions survive the
toggle); next / prev difference in document order; changes-only models (values not loaded,
truncated change lists, ``diff_nodes`` of big nodes on both core backends); big arrays (layout
cost, worker layout jobs); and a randomized consistency property: walking every difference
with ``next_change`` visits exactly the core's changes at their paths, and every row shown as
equal pairs canonically equal values.
"""

from __future__ import annotations

import copy
import random
import time
from typing import Any

import pytest

import jsongen
from jsonreshape import codec, reader
from jsonreshape.diff import Change, DiffOptions, ValueDiff, diff_nodes, diff_values
from jsonreshape.dupes import canonical_bytes
from jsonreshape.gui import difftree
from jsonreshape.gui.difftree import ABSENT, UNKNOWN, DiffTreeModel, char_spans, preview_text, value_kind
from jsonreshape.paths import MISSING
from jsonreshape.reader import Document


def J(text: str) -> Any:
    return codec.loads(text)


def model(a: Any, b: Any, **kw: Any) -> DiffTreeModel:
    opts = kw.pop("opts", None)
    vd = diff_values(a, b, opts or DiffOptions(max_changes=None)) if "vd" not in kw else kw.pop("vd")
    return DiffTreeModel(a, b, vd, opts=opts, **kw)


def rows(m: DiffTreeModel) -> list[tuple]:
    return [(r.depth, r.key_label, r.a_text, r.b_text, r.status) for r in m.iter_rows()]


def walk_changes(m: DiffTreeModel) -> list[int]:
    out: list[int] = []
    row = m.next_change(-1)
    guard = 0
    while row is not None:
        out.append(row)
        row = m.next_change(row)
        guard += 1
        assert guard < 100_000
    return out


# ----------------------------------------------------------------------------- helpers
def test_value_kind_and_preview() -> None:
    assert [value_kind(v) for v in ({}, [], "x", 1, 1.5, True, None)] == [
        "object", "array", "string", "number", "number", "bool", "null"]
    assert value_kind(ABSENT) is None and value_kind(UNKNOWN) is None
    assert preview_text(J('"a\\nb\\"c"')) == '"a\\nb\\"c"'
    assert preview_text(J("1.10")) == "1.10"
    assert preview_text(J("1e400")) == "1e400"
    assert preview_text(J("12345678901234567890")) == "12345678901234567890"
    assert preview_text(J("-0.0")) == "-0.0"
    assert preview_text(J('{"id": 1, "name": "Ali", "t": [1], "o": {}}')) == '{4} id: 1, name: "Ali", t: […], o: {}'
    assert preview_text(J("[1, 2]")) == "[2] 1, 2"
    assert preview_text([]) == "[]" and preview_text({}) == "{}"
    assert preview_text(ABSENT) == ""
    assert preview_text(UNKNOWN, "object") == "{…}" and preview_text(UNKNOWN, "array") == "[…]"
    long = "x" * 1000
    text = preview_text(long)
    assert text.startswith('"xxx') and text.endswith("…") and len(text) < 320
    # Uzbek Latin / Cyrillic and apostrophes are shown as they are
    assert preview_text("Oʻzbekiston G'ulom Тошкент") == '"Oʻzbekiston G\'ulom Тошкент"'


def test_char_spans() -> None:
    assert char_spans("1.10", "1.1") == (((3, 4),), ())
    sa, sb = char_spans('"Toshkent"', '"Toshkant"')
    assert sa == ((6, 7),) and sb == ((6, 7),)
    assert char_spans("abc", "abc") == ((), ())
    assert char_spans("abcdef", "uvwxyz") == ((), ())        # nothing in common: no highlights
    assert char_spans("a" * 500, "b") == ((), ())             # too long


# ----------------------------------------------------------------------------- objects
def test_equal_values_one_unchanged_tree(lang_uz) -> None:
    a = J('{"id": 1, "o": {"x": 1, "y": [1, 2]}, "n": 1.0}')
    b = J('{"n": 1, "o": {"y": [1, 2.0], "x": 1}, "id": 1}')      # other member order, 1 = 1.0
    m = model(a, b)
    assert m.equal and m.total_changes == 0 and m.describe() == "Farq yo'q"
    assert m.next_change(-1) is None and m.prev_change(-1) is None
    got = rows(m)
    assert got[0][:2] == (0, "$") and got[0][4] == "equal"
    assert [r[1] for r in got[1:]] == ["id", "o", "n"]          # A's member order
    o_row = m.row(2)
    assert o_row.status == "equal" and o_row.expander == 1 and o_row.summary
    assert o_row.a_text == "{…} 2 ta maydon, o'zgarishsiz"
    assert m.row(3).a_text == "1.0" and m.row(3).b_text == "1"    # shown as written, equal by value


def test_object_changes_statuses_and_order(lang_en) -> None:
    a = J('{"id": 7, "name": "Ali", "fax": "123", "city": "Toshkent"}')
    b = J('{"id": 7, "name": "Vali", "city": "Toshkent", "tags": ["vip"]}')
    m = model(a, b)
    assert m.counts == {"added": 1, "removed": 1, "changed": 1, "moved": 0}
    assert m.describe() == "1 changed, 1 added, 1 removed, 0 moved"
    got = rows(m)
    assert got == [
        (0, "$", got[0][2], got[0][3], "contains"),
        (1, "id", "7", "7", "equal"),
        (1, "name", '"Ali"', '"Vali"', "changed"),
        (1, "fax", '"123"', "", "removed"),
        (1, "city", '"Toshkent"', '"Toshkent"', "equal"),
        (1, "tags", "", '[1] "vip"', "added"),
    ]
    r_name, r_fax, r_tags = m.row(2), m.row(3), m.row(5)
    assert (r_name.role, r_fax.role, r_tags.role) == ("diff_changed", "diff_removed", "diff_added")
    assert r_name.point and r_fax.point and r_tags.point and not m.row(1).point
    assert (r_fax.a_path, r_fax.b_path) == (("fax",), None)
    assert (r_tags.a_path, r_tags.b_path) == (None, ("tags",))
    assert r_name.a_spans and r_name.b_spans                    # "Ali" vs "Vali": highlighted characters
    # the added container can be expanded: its children are tinted, but no separate differences
    assert r_tags.expander == 1
    m.expand(5)
    child = m.row(6)
    assert (child.key_label, child.b_text, child.status, child.point, child.role) == (
        "[0]", '"vip"', "added", False, "diff_added")
    assert walk_changes(m) == [2, 3, 5]
    assert m.change_at(2).op == "changed" and m.change_at(3).op == "removed" and m.change_at(5).op == "added"


def test_nested_containers_expanded_and_unchanged_collapsed() -> None:
    a = J('{"a": {"b": {"c": 1, "d": 2}}, "same": {"x": [1, 2, 3]}, "z": 0}')
    b = J('{"a": {"b": {"c": 1, "d": 3}}, "same": {"x": [1, 2, 3]}, "z": 0}')
    m = model(a, b)
    labels = [(r.depth, r.key_label, r.status, r.expander) for r in m.iter_rows()]
    assert labels == [
        (0, "$", "contains", 2),
        (1, "a", "contains", 2),
        (2, "b", "contains", 2),
        (3, "c", "equal", 0),
        (3, "d", "changed", 0),
        (1, "same", "equal", 1),          # unchanged subtree: one collapsed row
        (1, "z", "equal", 0),
    ]
    assert m.row(1).changes == 1 and m.row(0).changes == 1
    m.collapse(1)
    assert m.row_count() == 4
    assert m.next_change(0) == 4 and m.row_count() == 7          # navigation expands again


def test_type_change_is_one_change_with_split_children() -> None:
    a = J('{"v": {"x": 1, "y": 2}}')
    b = J('{"v": [10]}')
    m = model(a, b)
    v = m.row(1)
    assert v.status == "changed" and v.point and v.a_kind == "object" and v.b_kind == "array"
    assert v.expander == 1 and m.total_changes == 1
    m.expand(1)
    kids = [(r.key_label, r.a_text, r.b_text, r.status, r.point) for r in m.iter_rows(2)]
    assert kids == [("x", "1", "", "removed", False), ("y", "2", "", "removed", False), ("[0]", "", "10", "added", False)]
    assert walk_changes(m) == [1]


def test_scalar_roots_and_numbers() -> None:
    m = model(J("1"), J("1.0"))
    assert m.equal and m.row_count() == 1 and m.row(0).status == "equal"
    m = model(J("-0"), J("0"))
    assert m.equal
    m = model(J("1e400"), J("2e400"))
    assert not m.equal and m.row(0).status == "changed" and (m.row(0).a_text, m.row(0).b_text) == ("1e400", "2e400")
    m = model(J("12345678901234567890"), J("12345678901234567891"))
    assert m.row(0).a_text == "12345678901234567890" and m.row(0).a_spans == ((19, 20),)
    assert m.next_change(-1) == 0 and m.next_change(0) is None and m.prev_change(0) is None
    # 1.10 vs 1.1: equal by value, different as text
    assert model(J("[1.10]"), J("[1.1]")).equal
    m = model(J("[1.10]"), J("[1.1]"), opts=DiffOptions(numbers="text", max_changes=None))
    row = m.row(1)
    assert row.status == "changed" and row.a_text == "1.10" and row.a_spans == ((3, 4),) and row.b_spans == ()
    assert model(J("1"), J("true")).row(0).status == "changed"      # 1 != true


def test_unicode_keys_and_values(lang_uz) -> None:
    a = {"Oʻzbekiston": "Toshkent", "kalit\nyangi": "a b", "": "bo'sh", "Тошкент": 1}
    b = {"Oʻzbekiston": "Toshkent shahri", "kalit\nyangi": "a b", "": "bo'sh", "Тошкент": 2}
    m = model(a, b)
    by_label = {r.key_label: r for r in m.iter_rows(1)}
    assert set(by_label) == {"Oʻzbekiston", '"kalit\\nyangi"', '""', "Тошкент"}
    assert by_label['"kalit\\nyangi"'].a_text == '"a\\u2028b"'
    assert by_label["Oʻzbekiston"].status == "changed" and by_label["Тошкент"].status == "changed"
    assert by_label['""'].a_text == "\"bo'sh\""


# ----------------------------------------------------------------------------- arrays
def test_auto_array_front_insertion_does_not_cascade() -> None:
    a = [{"id": i, "v": "x" * 3} for i in range(6)]
    b = [{"id": 99, "v": "new"}] + copy.deepcopy(a)
    b[4]["v"] = "yyy"
    m = model(a, b)
    assert m.counts == {"added": 1, "removed": 0, "changed": 1, "moved": 0}
    kids = [(r.key_label, r.status) for r in m.iter_rows(1) if r.depth == 1]
    assert kids == [("[0]", "added"), ("[0 → 1]", "equal"), ("[1 → 2]", "equal"), ("[2 → 3]", "equal"),
                    ("[3 → 4]", "contains"), ("[4 → 5]", "equal"), ("[5 → 6]", "equal")]
    changed = m.row(m.next_change(m.next_change(-1)))
    assert (changed.key_label, changed.a_path, changed.b_path, changed.a_text, changed.b_text) == (
        "v", (3, "v"), (4, "v"), '"xxx"', '"yyy"')


def test_index_matching() -> None:
    a, b = [1, 2, 3], [0, 1, 2, 3]
    m = model(a, b, opts=DiffOptions(array_match="index", max_changes=None))
    kids = [(r.key_label, r.a_text, r.b_text, r.status) for r in m.iter_rows(1)]
    assert kids == [("[0]", "1", "0", "changed"), ("[1]", "2", "1", "changed"), ("[2]", "3", "2", "changed"),
                    ("[3]", "", "3", "added")]


def test_key_matching_reports_moves() -> None:
    a = J('[{"id": 1, "v": "a"}, {"id": 2, "v": "b"}, {"id": 3, "v": "c"}, {"id": 4, "v": "d"}]')
    b = J('[{"id": 3, "v": "c"}, {"id": 1, "v": "a"}, {"id": 2, "v": "B"}, {"id": 5, "v": "e"}]')
    opts = DiffOptions(array_match="key", array_keys=("id",), max_changes=None)
    m = model(a, b, opts=opts)
    assert m.counts["moved"] >= 1
    kids = [(r.key_label, r.status) for r in m.iter_rows(1) if r.depth == 1]
    # every A element at its A position; the move of id=3 is shown on it; id=4 removed, id=5 added
    assert kids == [("[0 → 1]", "equal"), ("[1 → 2]", "contains"), ("[2 → 0]", "moved"), ("[3]", "removed"),
                    ("[3]", "added")]
    moved = next(r for r in m.iter_rows() if r.status == "moved")
    assert moved.role == "diff_moved" and moved.point and moved.a_path == (2,) and moved.b_path == (0,)


def test_unordered_matching_pairs_equal_values(lang_en) -> None:
    a = J('[{"k": 1}, {"k": 2}, 3, 3, "x"]')
    b = J('[3, "x", {"k": 2}, {"k": 1}, 4]')
    opts = DiffOptions(array_match="unordered", max_changes=None)
    m = model(a, b, opts=opts)
    assert m.counts == {"added": 1, "removed": 1, "changed": 0, "moved": 0}
    kids = [(r.key_label, r.a_text, r.b_text, r.status) for r in m.iter_rows(1) if r.depth == 1]
    same = "{…} 1 field, unchanged"
    assert kids == [("[0 → 3]", same, same, "equal"), ("[1 → 2]", same, same, "equal"),
                    ("[2 → 0]", "3", "3", "equal"), ("[3]", "3", "", "removed"), ("[4 → 1]", '"x"', '"x"', "equal"),
                    ("[4]", "", "4", "added")]
    m.expand(1)
    assert [(r.key_label, r.a_text, r.b_text) for r in m.iter_rows(2, 3)] == [("k", "1", "1")]


# ----------------------------------------------------------------------------- only differences / navigation
def test_only_changes_gaps_and_expansion_survives(lang_en) -> None:
    a = {"items": [{"id": i, "v": i} for i in range(50)], "meta": {"x": 1}, "z": 1}
    b = copy.deepcopy(a)
    b["items"][10]["v"] = -1
    b["items"][40]["v"] = -2
    m = model(a, b)
    full_rows = m.row_count()
    assert m.set_only_changes(True) and m.only_changes
    labels = [(r.depth, r.key_label, r.status) for r in m.iter_rows()]
    assert labels == [
        (0, "$", "contains"),
        (1, "items", "contains"),
        (2, "… 10 unchanged", "gap"),
        (2, "[10]", "contains"),
        (3, "… 1 unchanged", "gap"),
        (3, "v", "changed"),
        (2, "… 29 unchanged", "gap"),
        (2, "[40]", "contains"),
        (3, "… 1 unchanged", "gap"),
        (3, "v", "changed"),
        (2, "… 9 unchanged", "gap"),
        (1, "… 2 unchanged", "gap"),
    ]
    gap = m.row(2)
    assert gap.gap == 10 and gap.expander == 0 and not gap.point and gap.role is None
    assert m.set_only_changes(False) and m.row_count() == full_rows   # the expansions came back
    assert not m.set_only_changes(False)


def test_next_prev_walk_in_document_order() -> None:
    a = J('{"a": 1, "b": {"c": [1, 2, 3], "d": "x"}, "e": [{"f": 1}], "g": 0}')
    b = J('{"a": 2, "b": {"c": [1, 9, 3], "d": "y"}, "e": [{"f": 1, "h": true}]}')
    m = model(a, b)
    points = walk_changes(m)
    paths = [m.row(r).path for r in points]
    assert paths == [("a",), ("b", "c", 1), ("b", "d"), ("e", 0, "h"), ("g",)]
    # backwards from the end gives the same rows reversed
    back = []
    row = m.prev_change(m.row_count())
    while row is not None:
        back.append(row)
        row = m.prev_change(row)
    assert back == points[::-1]
    # from a collapsed state navigation expands what it needs
    m.collapse_all()
    assert [r.key_label for r in m.iter_rows()] == ["$", "a", "b", "e", "g"]     # the top row stays open
    first = m.next_change(-1)
    assert m.row(first).path == ("a",)
    row = m.next_change(first)
    assert m.row(row).path == ("b", "c", 1)
    assert m.row(m.prev_change(row)).path == ("a",)
    assert m.prev_change(first) is None


def test_expand_all_and_collapse_all() -> None:
    a = {"x": [{"y": {"z": i}} for i in range(5)], "w": {"q": 1}}
    b = copy.deepcopy(a)
    b["x"][3]["y"]["z"] = 100
    b["new"] = {"deep": {"er": 1}}
    m = model(a, b, initial_rows=1)                 # nothing beyond the top row on creation
    assert m.row_count() == 4 and m.row(0).expander == 2
    assert m.expand_all()
    labels = [r.key_label for r in m.iter_rows()]
    assert "z" in labels and "deep" in labels          # the added container is opened one level
    assert "er" not in labels
    m.collapse_all()
    assert [r.key_label for r in m.iter_rows()] == ["$", "x", "w", "new"]


def test_moved_with_inner_changes_navigates_inside() -> None:
    a = J('[{"id": 1, "v": 1}, {"id": 2, "v": 2}]')
    b = J('[{"id": 2, "v": 2}, {"id": 1, "v": 5}]')
    m = model(a, b, opts=DiffOptions(array_match="key", array_keys=("id",), max_changes=None))
    points = walk_changes(m)
    statuses = [(m.row(r).status, m.row(r).path) for r in points]
    assert ("moved", (0,)) in statuses or ("moved", (1,)) in statuses
    assert ("changed", (0, "v")) in statuses


# ----------------------------------------------------------------------------- lone / changes-only models
def test_lone_record_value() -> None:
    v = J('{"id": 5, "tags": ["a", "b"]}')
    vd = ValueDiff()
    vd.counts["removed"] = 1
    vd.changes.append(Change("removed", (), None, v, MISSING))
    m = DiffTreeModel(v, ABSENT, vd, root_label="#5")
    root = m.row(0)
    assert (root.key_label, root.status, root.point, root.b_text) == ("#5", "removed", True, "")
    kids = [(r.key_label, r.status, r.point) for r in m.iter_rows(1)]
    assert kids == [("id", "removed", False), ("tags", "removed", False)]
    assert walk_changes(m) == [0]


def test_changes_only_model_from_a_value_diff(lang_en) -> None:
    a = {"big": list(range(100)), "k": {"x": 1, "y": [1, 2]}, "same": 1}
    b = copy.deepcopy(a)
    b["big"][50] = -1
    b["k"]["y"].append(3)
    del b["k"]["x"]
    vd = diff_values(a, b)
    m = DiffTreeModel(UNKNOWN, UNKNOWN, vd, a_kind="object", b_kind="object")
    assert not m.complete and m.only_changes and not m.can_show_all
    assert not m.set_only_changes(False) and m.only_changes        # forced
    got = [(r.depth, r.key_label, r.a_text, r.b_text, r.status) for r in m.iter_rows()]
    assert got == [
        (0, "$", "{…}", "{…}", "contains"),
        (1, "big", "[…]", "[…]", "contains"),
        (2, "[50]", "50", "-1", "changed"),
        (1, "k", "{…}", "{…}", "contains"),
        (2, "x", "1", "", "removed"),
        (2, "y", "[…]", "[…]", "contains"),
        (3, "[2]", "", "3", "added"),
    ]
    assert [m.row(r).path for r in walk_changes(m)] == [("big", 50), ("k", "x"), ("k", "y", 2)]


def test_truncated_change_list_gives_changes_only_rows() -> None:
    a = list(range(30))
    b = [x + 1 for x in a]
    vd = diff_values(a, b, DiffOptions(array_match="index", max_changes=5))
    assert vd.truncated
    m = DiffTreeModel(a, b, vd, opts=DiffOptions(array_match="index"))
    assert not m.complete and m.only_changes
    assert [r.key_label for r in m.iter_rows(1)] == ["[0]", "[1]", "[2]", "[3]", "[4]"]
    assert m.row(1).a_text == "0" and m.row(1).b_text == "1"     # values still shown where known
    assert m.counts["changed"] == 30


@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    yield name
    reader.set_scanner_backend(old)


def test_diff_nodes_of_big_nodes_changes_only(tmp_path, backend) -> None:
    """Nodes compared through the Node API (memory-bounded): the model shows the changed paths
    (and the Omitted text of a value too large to hold)."""
    from jsonreshape.settings import Settings

    recs_a = [{"id": i, "name": f"n{i}", "nums": J("[1.10, 1e400, 12345678901234567890, -0.0]")} for i in range(300)]
    recs_b = copy.deepcopy(recs_a)
    recs_b[7]["name"] = "Тошкент"
    recs_b.insert(100, {"id": -1})
    pa, pb = tmp_path / "a.json", tmp_path / "b.json"
    jsongen.write_file(pa, recs_a, "array")
    jsongen.write_file(pb, recs_b, "array")
    settings = Settings(large_node_bytes=1024)          # every node bigger than 1 KiB goes the Node path
    da = Document.open(pa, settings=settings)
    db = Document.open(pb, settings=settings)
    try:
        da.build_index()
        db.build_index()
        vd = diff_nodes(da, da.root(), db, db.root(), DiffOptions())
        m = DiffTreeModel(UNKNOWN, UNKNOWN, vd, a_kind="array", b_kind="array")
        paths = [(m.row(r).status, m.row(r).path) for r in walk_changes(m)]
        assert paths == [("changed", (7, "name")), ("added", (100,))] or paths == [
            ("changed", (7, "name")), ("added", None)]
        name_row = next(r for r in m.iter_rows() if r.status == "changed")
        assert name_row.b_text == '"Тошкент"' and name_row.a_text == '"n7"'
        added = next(r for r in m.iter_rows() if r.status == "added")
        assert added.b_path == (100,) and added.b_text == "{1} id: -1"
    finally:
        da.close()
        db.close()


# ----------------------------------------------------------------------------- big arrays
def test_big_ordered_array_is_cheap() -> None:
    n = 200_000
    a = list(range(n))
    b = list(range(n))
    b[123_456] = -5
    b.insert(1000, "new")
    start = time.perf_counter()
    m = model(a, b)
    built = time.perf_counter() - start
    assert m.row_count() == 1 + n + 1
    first = m.next_change(-1)
    second = m.next_change(first)
    assert (m.row(first).key_label, m.row(first).status) == ("[1000]", "added")
    assert (m.row(second).key_label, m.row(second).status) == ("[123456 → 123457]", "changed")
    assert m.row(n).key_label == f"[{n - 2} → {n - 1}]"
    lay = m._layout(m._root)
    assert len(lay.pieces) <= 6                    # runs of equal pairs, not one piece per element
    m.set_only_changes(True)
    assert m.row_count() == 1 + 5
    assert built < 5.0


def test_big_unordered_array_needs_a_layout_job() -> None:
    n = difftree.SYNC_UNORDERED_MAX
    a = {"x": list(range(n))}
    b = {"x": list(reversed(range(n)))}
    b["x"][0] = -1                                  # n-1 replaced by -1
    opts = DiffOptions(array_match="unordered", max_changes=None)
    m = model(a, b, opts=opts)
    assert m.row_count() == 2 and not m.is_expanded(1)    # not laid out on creation
    assert m.expand(1) == -1                        # expensive: a worker lays it out
    assert m.next_change(-1) is None or m.row(m.next_change(-1)).key_label == "x"
    job = m.layout_job(1)
    assert job is not None and m.layout_job(0) is None
    job.run(None)
    row = m.apply_job(job)
    assert row == 1 and m.is_expanded(1) and m.row_count() == 2 + n + 1
    kids = {r.key_label: r.status for r in m.iter_rows(2)}
    assert kids[f"[{n - 1}]"] == "removed" and kids["[0]"] == "added"
    assert kids[f"[0 → {n - 1}]"] == "equal"
    assert [m.row(r).path for r in walk_changes(m)] == [("x", n - 1), ("x", 0)]
    # a stale job (the model was rebuilt) is ignored
    job2 = difftree.LayoutJob(job.ident, job.epoch - 1, lambda c: [])
    job2.run()
    assert m.apply_job(job2) is None


def test_array_fields() -> None:
    a = {"orders": [{"oid": 1, "items": [{"sku": "x"}]}], "x": 1}
    b = {"orders": [{"oid": 2, "note": "n"}]}
    m = model(a, b)
    assert m.array_fields() == ["items", "note", "oid", "sku"]


def test_reveal_paths() -> None:
    a = {"a": {"b": [0, {"c": 1}]}}
    b = copy.deepcopy(a)
    m = model(a, b, initial_rows=1)
    row = m.reveal_paths(("a", "b", 1, "c"))
    assert row is not None and m.row(row).key_label == "c" and m.row(row).a_path == ("a", "b", 1, "c")
    assert m.reveal_paths(("nope",)) is None


# ----------------------------------------------------------------------------- consistency property
def _mutate(rng: random.Random, v: Any, depth: int = 0) -> Any:
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            r = rng.random()
            if r < 0.08:
                continue                                # removed member
            out[k] = _mutate(rng, x, depth + 1) if r < 0.5 else x
        if rng.random() < 0.15:
            out[f"new{rng.randrange(100)}"] = jsongen.rand_value(rng, depth + 1, 3, 3)
        return out
    if isinstance(v, list):
        out = [(_mutate(rng, x, depth + 1) if rng.random() < 0.4 else x) for x in v]
        if out and rng.random() < 0.2:
            del out[rng.randrange(len(out))]
        if rng.random() < 0.2:
            out.insert(rng.randrange(len(out) + 1), jsongen.rand_value(rng, depth + 1, 2, 3))
        if len(out) > 1 and rng.random() < 0.15:
            i, j = rng.randrange(len(out)), rng.randrange(len(out))
            out[i], out[j] = out[j], out[i]
        return out
    r = rng.random()
    if r < 0.3:
        return jsongen.rand_scalar(rng)
    if r < 0.35:
        return {"t": v}
    return v


def _keyed_array(rng: random.Random, n: int) -> list:
    return [{"id": i, "v": jsongen.rand_scalar(rng)} for i in range(n)]


def _multiset_bytes(v: Any) -> bytes:
    """Canonical bytes with every array taken as a multiset (unordered matching)."""
    if isinstance(v, list):
        return b"[" + b",".join(sorted(_multiset_bytes(x) for x in v)) + b"]"
    if isinstance(v, dict):
        return b"{" + b",".join(sorted(canonical_bytes(k) + b":" + _multiset_bytes(x) for k, x in v.items())) + b"}"
    return canonical_bytes(v)


def _check_consistency(a: Any, b: Any, opts: DiffOptions) -> None:
    vd = diff_values(a, b, DiffOptions(array_match=opts.array_match, array_keys=opts.array_keys,
                                       numbers=opts.numbers, max_changes=None))
    m = DiffTreeModel(a, b, vd, opts=opts, initial_rows=10)
    seen: list[Change] = []
    for row in walk_changes(m):
        r = m.row(row)
        ch = m.change_at(row)
        assert ch is not None and r.point, (r, a, b)
        seen.append(ch)
        assert (r.a_path, r.b_path) == ((ch.a_path, ch.b_path) if ch.op != "added" else (None, ch.b_path)), (r, ch)
    expected = list(vd.changes)
    assert len(seen) == len(expected), (len(seen), len(expected), a, b)
    assert {id(c) for c in seen} == {id(c) for c in expected}
    # rows shown as equal pair canonically equal values (the implicit pairing is right)
    m.expand_all(max_nodes=2000, max_rows=20_000)
    for r in m.iter_rows():
        if r.status == "equal" and r.a_path is not None and r.b_path is not None:
            va, vb = m.values_at(r.row)
            if opts.array_match == "unordered":
                assert _multiset_bytes(va) == _multiset_bytes(vb), (r, va, vb)
            elif opts.numbers == "value":
                assert canonical_bytes(va) == canonical_bytes(vb), (r, va, vb)


@pytest.mark.parametrize("match", ["auto", "index", "unordered"])
def test_random_pairs_are_shown_consistently(match: str) -> None:
    rng = random.Random(f"difftree-{match}")
    opts = DiffOptions(array_match=match, max_changes=None)
    for _ in range(120):
        a = jsongen.rand_value(rng, 0, 4, 5)
        b = _mutate(rng, a)
        _check_consistency(a, b, opts)


def test_random_key_matched_arrays_are_shown_consistently() -> None:
    rng = random.Random("difftree-key")
    opts = DiffOptions(array_match="key", array_keys=("id",), max_changes=None)
    for _ in range(80):
        a = _keyed_array(rng, rng.randrange(0, 12))
        b = copy.deepcopy(a)
        rng.shuffle(b)
        for x in b:
            if rng.random() < 0.3:
                x["v"] = jsongen.rand_scalar(rng)
        if b and rng.random() < 0.5:
            del b[rng.randrange(len(b))]
        if rng.random() < 0.5:
            b.insert(rng.randrange(len(b) + 1), {"id": 1000 + rng.randrange(5), "v": 0})
        _check_consistency({"list": a}, {"list": b}, opts)


def test_random_text_numbers_are_shown_consistently() -> None:
    rng = random.Random("difftree-text")
    opts = DiffOptions(numbers="text", max_changes=None)
    tokens = ["1", "1.0", "1.10", "1e0", "-0", "0", "1e400", "12345678901234567890"]
    for _ in range(60):
        a = [codec.loads(rng.choice(tokens)) for _ in range(rng.randrange(1, 8))]
        b = [codec.loads(rng.choice(tokens)) for _ in range(rng.randrange(1, 8))]
        _check_consistency({"n": a}, {"n": b}, opts)
