"""The structure builder's model functions (jsonreshape/gui/mapmodel.py; docs/ARCHITECTURE.md §13.5).

* ``visible_rows``: the prototype's walkRender (open nodes, element contexts, 400 children + a "more" row, limits);
* ``row_info`` / ``MapModel``: texts, chips, badges, actions, the editable flag, renames through a signal;
* ``preview_text``: the prototype's fmt (two-space indent, 20 items + "… davomi eksportda" / "… yana N ta", 200 keys
  + "… yana N ta kalit", strings cut at 300 characters, exact numbers, null for MISSING), iterative for deep values,
  bounded (characters / lines), lone surrogates and line separators escaped;
* ``place_records`` / ``compute_preview`` for every document kind (records, wrapped, keyed incl. a member that is
  not written, single), the size limit and the caches; ``build_tree`` on the sample positions of
  ``schema.sample_positions`` (huge records counted and left out) and on every record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from jsonreshape import codec, engine, schema  # noqa: E402
from jsonreshape import mapping as mp  # noqa: E402
from jsonreshape.errors import UserInputError  # noqa: E402
from jsonreshape.gui import mapmodel  # noqa: E402
from jsonreshape.gui.mapmodel import (  # noqa: E402
    MORE_STEP,
    MapModel,
    build_tree,
    compute_preview,
    display_text,
    place_records,
    preview_text,
    row_info,
    visible_rows,
)
from jsonreshape.reader import Document  # noqa: E402
from jsonreshape.settings import Settings  # noqa: E402

NOLIMIT = {"max_keys": None, "max_depth": None, "max_nodes": None}


def tree_of(values: list, **kw: Any) -> mp.MappingTree:
    return mp.infer_tree(values, **{**NOLIMIT, **kw})


def labels(tree: mp.MappingTree, rows: list) -> list[str]:
    out = []
    for r in rows:
        if r.kind == "more":
            out.append(f"{'  ' * r.depth}+{r.hidden}")
        else:
            n = r.node
            out.append("  " * r.depth + ("(root)" if n.is_root else "[]" if n.is_elem else n.key))
    return out


# ----------------------------------------------------------------------------------------
# rows
# ----------------------------------------------------------------------------------------
def test_visible_rows_follow_open_state_and_contexts() -> None:
    tree = tree_of([{"a": 1, "o": {"x": [{"y": 1}]}}])
    rows = visible_rows(tree)
    assert labels(tree, rows) == ["(root)", "  []", "    a", "    o", "      x", "        []", "          y"]
    y = rows[-1]
    assert y.ctx == ((None,), (None, "o", "x", None)) and y.parent.is_elem
    assert rows[0].ctx == () and rows[0].parent is None
    d = mp.Designer(tree, undo_limit=10)
    d.set_open(tree.find("[].o").id, False)
    assert labels(tree, visible_rows(tree)) == ["(root)", "  []", "    a", "    o"]
    d.collapse_all()
    assert labels(tree, visible_rows(tree)) == ["(root)", "  []", "    a", "    o"]
    d.set_open(tree.find("").id, False)
    assert labels(tree, visible_rows(tree)) == ["(root)"]


def test_visible_rows_paginate_400_children() -> None:
    tree = tree_of([{f"k{i}": i for i in range(MORE_STEP * 2 + 5)}])
    rows = visible_rows(tree)
    assert len(rows) == 2 + MORE_STEP + 1
    more = rows[-1]
    assert more.kind == "more" and more.hidden == MORE_STEP + 5 and more.node_id == tree.find("[]").id
    assert more.depth == 2 and more.node is None
    rows = visible_rows(tree, {more.node_id: MORE_STEP * 2})
    assert rows[-1].kind == "more" and rows[-1].hidden == 5
    rows = visible_rows(tree, {more.node_id: MORE_STEP * 3})
    assert all(r.kind == "node" for r in rows) and len(rows) == 2 + MORE_STEP * 2 + 5


def test_visible_rows_of_a_deep_tree_are_iterative() -> None:
    value: Any = 1
    for _ in range(3000):
        value = {"d": value}
    tree = mp.infer_tree([value], max_depth=None)
    d = mp.Designer(tree, undo_limit=10, max_depth=None)
    d.expand_all()
    rows = visible_rows(tree)
    assert len(rows) == 3002 and rows[-1].depth == 3001


def test_row_info_texts_and_the_model(qtbot) -> None:
    data = [{"id": 1, "na\nme": "a\nb", "orders": [{"oid": 7}]}, {"id": 2}]
    tree = tree_of(data)
    d = mp.Designer(tree, undo_limit=10)
    d.move(tree.find("[].orders[].oid").id, tree.find("[].id").id, "after")
    rows = visible_rows(tree)
    infos = {r.node.key: row_info(tree, d, r, file_name="f.json", record_count=2)
             for r in rows if r.kind == "node" and not r.node.is_elem}
    assert infos[""].label == "f.json" and infos[""].meta == [("2 ta yozuv", "")] and infos[""].style == "root"
    oid = infos["oid"]
    assert [b[0] for b in oid.badges] == ["ko'chirildi", "ro'yxatga yig'iladi"]
    assert oid.meta == [] and oid.draggable and oid.renamable
    assert infos["orders"].meta == [("50%", "1 / 2 ta obyektda bor")]
    assert infos["na\nme"].sample == '"a\\nb"' and infos["na\nme"].label == "na\\u000ame"
    assert display_text("a\nb\u202ec\ud800") == "a\\u000ab\\u202ec\\ud800"
    assert display_text("Oʻzbekiston") == "Oʻzbekiston"
    # the model: rows, flags, rename requests
    model = MapModel()
    model.set_rows(rows, lambda r: row_info(tree, d, r, file_name="f.json", record_count=2))
    assert model.rowCount() == len(rows)
    from PySide6.QtCore import Qt

    root_index = model.index(0, 0)
    assert not (model.flags(root_index) & Qt.ItemFlag.ItemIsEditable)
    name_id = tree.find([0, "na\nme"]).id
    index = model.index(model.row_of(name_id), 0)
    assert model.flags(index) & Qt.ItemFlag.ItemIsEditable
    assert model.data(index, Qt.ItemDataRole.EditRole) == "na\nme"
    assert model.data(index, Qt.ItemDataRole.DisplayRole) == "na\\u000ame"
    with qtbot.waitSignal(model.rename_requested, timeout=1000) as blocker:
        assert model.setData(index, "ism") is False
    assert blocker.args == [name_id, "ism"]
    assert model.row_of(None) is None and model.row(10_000) is None
    access = model.data(model.index(model.row_of(tree.find("[].oid").id), 0), Qt.ItemDataRole.AccessibleTextRole)
    assert "ko'chirildi" in access


# ----------------------------------------------------------------------------------------
# preview text
# ----------------------------------------------------------------------------------------
def test_preview_text_markers_and_exact_numbers() -> None:
    value = codec.loads(b'{"n": 1.10, "e": 1e400, "big": 12345678901234567890, "neg": -0.0, "z": 0.1, '
                        b'"t": true, "f": false, "x": null, "e2": [], "o": {}}')
    assert preview_text(value) == ('{\n  "n": 1.10,\n  "e": 1e400,\n  "big": 12345678901234567890,\n  "neg": -0.0,\n'
                                   '  "z": 0.1,\n  "t": true,\n  "f": false,\n  "x": null,\n  "e2": [],\n  "o": {}\n}')
    items = list(range(25))
    out = preview_text(items, mode="out")
    assert out.endswith("  19,\n  … davomi eksportda\n]") and "20," not in out
    src = preview_text(items, mode="src")
    assert src.endswith("  19,\n  … yana 5 ta\n]")
    obj = {f"k{i}": i for i in range(203)}
    assert preview_text(obj).endswith('  "k199": 199,\n  … yana 3 ta kalit\n}')
    s = "ж" * 301
    assert preview_text(s) == '"' + "ж" * 300 + '…"'
    assert preview_text("ab c\ud800") == '"ab\\u2028c\\ud800"'
    assert preview_text({"a\nb": "Oʻzbekiston 'Ўзбекистон'"}) == '{\n  "a\\nb": "Oʻzbekiston \'Ўзбекистон\'"\n}'
    assert preview_text(engine.MISSING) == "null" and preview_text(None) == "null"
    assert preview_text([[1, [2]], {"a": [3]}]) == '[\n  [\n    1,\n    [\n      2\n    ]\n  ],\n  {\n    "a": [\n' \
                                                   '      3\n    ]\n  }\n]'


def test_preview_text_is_iterative_and_bounded() -> None:
    value: Any = 1
    for _ in range(1500):                     # deeper than the recursion limit
        value = [value]
    out = preview_text(value, max_chars=10_000_000, max_lines=1_000_000)
    assert out.count("[") == 1500 and out.splitlines()[1500].strip() == "1"
    wide = [[i] * 20 for i in range(20)]
    cut = preview_text(wide, max_lines=50)
    assert cut.endswith("… ko'rinish shu yerda qisqartirildi (eksportda hammasi chiqadi)")
    assert cut.count("\n") <= 60
    cut = preview_text("x" * 100 + "y", max_chars=50)
    assert '"' + "x" * 100 + 'y"' in cut      # one piece may pass the limit, nothing more is added after it


def test_place_records() -> None:
    w = {"meta": 1, "a": [{"b": {"items": []}}]}
    out = place_records(w, ("a", 0, "b", "items"), [{"r": 1}])
    assert out == {"meta": 1, "a": [{"b": {"items": [{"r": 1}]}}]} and w["a"][0]["b"]["items"] == []
    assert place_records(w, (), [1]) == [1]
    with pytest.raises(ValueError):
        place_records(w, ("nope",), [])
    with pytest.raises(ValueError):
        place_records(w, ("meta", "x"), [])


# ----------------------------------------------------------------------------------------
# compute_preview / build_tree on real documents
# ----------------------------------------------------------------------------------------
def open_doc(path: Path, **kw: Any) -> Document:
    doc = Document.open(path, use_cache=False, **kw)
    doc.build_index()
    return doc


def test_compute_preview_for_every_kind(tmp_path: Path) -> None:
    recs = [{"id": i, "tags": list(range(30)), "s": "Тошкент"} for i in range(4)]
    cases = {
        "array": (recs, {}),
        "wrapped": ({"meta": {"v": 1}, "data": recs, "tail": True}, {}),
        "keyed": ({f"k{i}": r for i, r in enumerate(recs)}, {}),
        "single": ({"title": "x", "items": [1, 2], "inner": {"a": 1}}, {}),
    }
    for kind, (value, kw) in cases.items():
        path = tmp_path / f"{kind}.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        doc = open_doc(path, **kw)
        try:
            assert doc.kind == kind
            pinned = doc.pinned()
            res = build_tree(pinned, sample=10000, full=False)
            tree = res.tree
            snap = tree.snapshot()
            rp = tuple(doc.record_path) if doc.record_path is not None else None
            out = compute_preview(pinned, kind=kind, record_path=rp, index=2, mode="out", snapshot=snap)
            src = compute_preview(pinned, kind=kind, record_path=rp, index=2, mode="src", snapshot=None)
            assert out.count == pinned.record_count and out.error == "" and out.message == ""
            if kind == "array":
                assert json.loads(out.text.replace("… davomi eksportda", '"more"')) == \
                    {"id": 2, "tags": list(range(20)) + ["more"], "s": "Тошкент"}
                assert "… yana 10 ta" in src.text
            elif kind == "wrapped":
                shown = json.loads(src.text.replace("… yana 10 ta", '"more"'))
                assert shown["data"][0]["id"] == 2 and len(shown["data"]) == 1 and shown["meta"] == {"v": 1}
                assert out.wrapper == {"meta": {"v": 1}, "data": [], "tail": True}
            elif kind == "keyed":
                assert json.loads(src.text.replace("… yana 10 ta", '"more"'))["k2"]["id"] == 2
                assert out.key == "k2"
            else:
                assert json.loads(out.text) == value and out.index == 0
            # caches: the same state + record gives the cached value back without reading
            again = compute_preview(pinned, kind=kind, record_path=rp, index=2, mode="out", snapshot=snap,
                                    value_cache=(out.value_key, "CACHED", out.key),
                                    wrapper_cache=(out.wrapper_key, out.wrapper) if out.wrapper_key else None)
            if kind in ("array", "keyed"):
                assert "CACHED" in again.text
        finally:
            doc.close()


def test_compute_preview_keyed_member_not_written_and_size_limit(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "k.json"
    path.write_text(json.dumps({"a": {"x": 1}, "b": {"y": 2}, "c": {"x": 3}}), encoding="utf-8")
    doc = open_doc(path)
    try:
        pinned = doc.pinned()
        tree = build_tree(pinned, sample=None, full=True).tree
        d = mp.Designer(tree, undo_limit=10)
        d.delete(tree.find("y").id)
        d.delete(tree.find("x").id)
        # every member value becomes {} (an object node of an object): written; a scalar member would be copied
        res = compute_preview(pinned, kind="keyed", record_path=None, index=1, mode="out", snapshot=tree.snapshot())
        assert json.loads(res.text) == {"b": {}}
        monkeypatch.setattr(mapmodel, "PREVIEW_MAX_BYTES", 4)
        with pytest.raises(UserInputError) as info:
            compute_preview(pinned, kind="keyed", record_path=None, index=1, mode="src", snapshot=None)
        assert info.value.key == "gui.mapview.preview.too_large"
    finally:
        doc.close()
    path = tmp_path / "empty.json"
    path.write_text("[]", encoding="utf-8")
    doc = open_doc(path)
    try:
        res = compute_preview(doc.pinned(), kind="array", record_path=None, index=0, mode="out", snapshot=b"")
        assert res.message == "Hujjatda yozuv yo'q." and res.count == 0
    finally:
        doc.close()


def test_compute_preview_member_missing_message(tmp_path: Path) -> None:
    path = tmp_path / "k.json"
    path.write_text(json.dumps({"a": {"x": {"q": 1}}, "b": {"x": {"q": 2}}}), encoding="utf-8")
    doc = open_doc(path)
    try:
        pinned = doc.pinned()
        tree = build_tree(pinned, sample=None, full=True).tree
        assert tree.root_kind == "keyed"
        # a tree whose root reads a source the member values do not have: MISSING -> not written
        root = tree.root
        root.kind = "value"
        root.src = ("nope",)
        root.children = []
        tree.touch()
        res = compute_preview(pinned, kind="keyed", record_path=None, index=0, mode="out", snapshot=tree.snapshot())
        assert res.message == "«a» a'zosi natijaga yozilmaydi: tuzilma unga qiymat bermaydi." and res.text == ""
    finally:
        doc.close()


def test_build_tree_uses_the_schema_sample_positions(tmp_path: Path) -> None:
    recs = [{"id": i, ("rare" if i == 1 else "id2"): i} for i in range(50)]
    big = {"id": 99, "blob": "z" * 6000, "only_in_big": 1}
    recs[10] = big
    path = tmp_path / "s.json"
    path.write_text(json.dumps(recs), encoding="utf-8")
    settings = Settings(large_node_bytes=4096)
    doc = Document.open(path, use_cache=False, settings=settings)
    doc.build_index()
    try:
        pinned = doc.pinned()
        res = build_tree(pinned, sample=10, full=False)
        positions = schema.sample_positions(50, 10)
        assert 1 not in positions and 10 in positions
        assert res.scanned == 9 and res.skipped == 1 and res.total == 50 and res.sample == 10 and not res.full
        assert res.tree.find("[].rare") is None and res.tree.find("[].only_in_big") is None
        assert res.tree.find("[]").count == 9
        full = build_tree(pinned, sample=None, full=True)
        assert full.full and full.scanned == 49 and full.skipped == 1 and full.tree.find("[].rare") is not None
        values = [pinned.record(i) for i in positions if i != 10]
        assert [n.key for n in res.tree.iter_nodes()] == [n.key for n in mp.infer_tree(values).iter_nodes()]
    finally:
        doc.close()


def test_population_reads_the_sample(tmp_path: Path) -> None:
    recs = [{"id": i, "children": [{"id": i * 10, "children": []}]} for i in range(30)]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(recs), encoding="utf-8")
    doc = open_doc(path)
    try:
        pinned = doc.pinned()
        pop = mapmodel.make_population(pinned, sample=5, holder={})
        assert [v["id"] for v in pop()] == [recs[i]["id"] for i in schema.sample_positions(30, 5)]
        assert [v["id"] for v in pop()] == [recs[i]["id"] for i in schema.sample_positions(30, 5)]   # again
        assert len(list(mapmodel.make_population(pinned, sample=None, holder={})())) == 30
        assert len(list(mapmodel.make_population(pinned, sample=30, holder={})())) == 30
    finally:
        doc.close()
    # huge records are not part of the population (like the inference); the single value is
    recs[3] = {"id": 3, "blob": "h" * 5000}
    path.write_text(json.dumps(recs), encoding="utf-8")
    doc = Document.open(path, use_cache=False, settings=Settings(large_node_bytes=4096))
    doc.build_index()
    try:
        ids = [v["id"] for v in mapmodel.make_population(doc.pinned(), sample=None, holder={})()]
        assert ids == [r["id"] for r in recs if r["id"] != 3]
    finally:
        doc.close()
    single = tmp_path / "one.json"
    single.write_text('{"n": 1.10, "children": [{"n": 2}]}', encoding="utf-8")
    doc = open_doc(single)
    try:
        assert doc.kind == "single"
        values = list(mapmodel.make_population(doc.pinned(), sample=10000, holder={})())
        assert len(values) == 1 and codec.dumps(values[0]) == b'{"n":1.10,"children":[{"n":2}]}'
    finally:
        doc.close()
