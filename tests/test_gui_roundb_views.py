"""Stage-5 round B, the views (tasks 2-7): the schema view's null / missing column, integer share and the
"Statistika" / "Takroriy yozuvlar…" entry points (signals with the field text, double-click), the table header's
"Statistika", "Tanlangan ikkitasini taqqoslash" in the tree and the table, ``gui/fields.py`` on the core schema
helpers (parity with the former code) and :func:`fields.store`, and the CL-9 regressions (value / text / full-value
tasks read the state they were asked for; a text build that finished after its cancel is never cached).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QMainWindow  # noqa: E402

from jsonreshape import schema as schema_mod  # noqa: E402
from jsonreshape.gui import comparewindow, fields, textview, widgets  # noqa: E402
from jsonreshape.gui.schemaview import (  # noqa: E402
    COL_COUNT,
    COL_DETAILS,
    COL_NULLS,
    COL_PRESENCE,
    COL_TYPES,
    SchemaView,
)
from jsonreshape.gui.session import NodeRef  # noqa: E402
from jsonreshape.gui.tableview import TableView  # noqa: E402
from jsonreshape.gui.textview import TextView  # noqa: E402
from jsonreshape.gui.treeview import TreeView  # noqa: E402
from jsonreshape.gui.widgets import FullValueDialog  # noqa: E402
from jsonreshape.i18n import t  # noqa: E402
from jsonreshape.paths import Wild  # noqa: E402
from jsonreshape.reader import Document  # noqa: E402
from jsonreshape.schema import infer_schema  # noqa: E402

from views_edit_helpers import (  # noqa: E402,F401
    TIMEOUT,
    app,
    applied,
    keep_selection,
    no_logged_errors,
    opened,
    records,
    views,
    write,
    write_json,
)

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("no_logged_errors")]


# ==================================================================================
# schema view (task 2)
# ==================================================================================
def people(n: int) -> str:
    """JSON text of n records; ``n`` cycles through the exact tokens 3, 1e400, 1.000, 2.5."""
    tokens = ("3", "1e400", "1.000", "2.5")
    out = []
    for i in range(n):
        rec = {"id": i, "age": None if i % 10 == 0 else i % 90, "contact": {"city": ["Toshkent", "Buxoro"][i % 2]},
               "orders": [{"sum": i + 0.5} if j % 2 else {"sum": None, "x": 1} for j in range(i % 3)]}
        if i % 3 == 0:
            rec["email"] = f"u{i}@x.uz"
        body = json.dumps(rec, ensure_ascii=False)
        out.append(body[:-1] + f', "n": {tokens[i % 4]}}}')
    return "[" + ",\n".join(out) + "]"


def schema_view(qtbot, s: Any) -> SchemaView:
    view = SchemaView(s)
    qtbot.addWidget(view)
    view.resize(1200, 600)
    view.show()
    with qtbot.waitSignal(view.schema_shown, timeout=TIMEOUT):
        view.activate()
    return view


def test_schema_nulls_missing_integers_sample_then_full(qtbot, app, opened, tmp_path: Path, lang_uz) -> None:
    s = opened(write(tmp_path / "p.json", people(300)))
    app.settings.sample_size = 50
    view = schema_view(qtbot, s)
    assert not view.schema.complete
    header = view.tree.headerItem()
    assert header.text(COL_NULLS) == "null / yo'q"
    assert "Namuna bo'yicha: 50 ta yozuv (jami 300)" in header.toolTip(COL_NULLS)
    age = view.item_for("age")
    assert "Namuna bo'yicha: 50 ta yozuv (jami 300)" in age.toolTip(COL_NULLS)
    assert "Namuna" in age.toolTip(COL_PRESENCE) and "null:" in age.toolTip(COL_PRESENCE)

    view.full_scan()
    qtbot.waitUntil(lambda: view.schema is not None and view.schema.complete, timeout=TIMEOUT)
    assert "Barcha 300 ta yozuv bo'yicha (to'liq skanerlash)." in view.tree.headerItem().toolTip(COL_NULLS)
    age = view.item_for("age")
    assert age.text(COL_NULLS) == "30 / 0"
    tip = age.toolTip(COL_NULLS)
    assert "null: 30 ta (qiymatlarning 10%)" in tip and "Yo'q: 0 / 300 (yozuvlar)" in tip and "Barcha 300" in tip
    assert "butun 100%" in age.text(COL_DETAILS)
    assert "shundan butun sonlar: 270 ta (100%)" in age.toolTip(COL_TYPES)
    email = view.item_for("email")
    assert email.text(COL_NULLS) == "0 / 200"
    # under an array: the context is the order element, null counts too
    osum = view.item_for("orders[*].sum")
    fi = view.schema.find("orders[*].sum")
    assert osum.text(COL_NULLS) == f"{fi.nulls} / {fi.missing}" and fi.nulls > 0
    assert "massiv elementlari" in osum.toolTip(COL_NULLS)
    assert "butun 0%" in osum.text(COL_DETAILS)
    # exact values: 3, 1e400 and 1.000 are integral, 2.5 is not
    n = view.item_for("n")
    assert view.schema.find("n").integers == 225 and "butun 75%" in n.text(COL_DETAILS)
    # a string field has no integer share; sorting by the new column works
    assert "butun" not in view.item_for("contact.city").text(COL_DETAILS)
    view.tree.sortByColumn(COL_NULLS, Qt.SortOrder.DescendingOrder)
    top = [view.tree.topLevelItem(i).fi.text for i in range(view.tree.topLevelItemCount())]
    assert top[0] == "email"                       # the most missing first
    assert view.item_for("id").text(COL_COUNT) == "300"
    assert app.errors == []


def test_schema_stats_and_dupes_signals_double_click(qtbot, app, opened, tmp_path: Path, lang_uz) -> None:
    s = opened(write(tmp_path / "p.json", people(60)))
    view = schema_view(qtbot, s)
    assert not view.act_stats.isEnabled() and not view.act_dupes.isEnabled()      # nothing selected
    view.select_field("orders[*].sum")
    assert view.act_stats.isEnabled() and view.act_stats.toolTip() == t("gui.statspanel.action.node.tip")
    with qtbot.waitSignal(view.stats_requested, timeout=1000) as sig:
        view.act_stats.trigger()
    assert sig.args == ["orders[*].sum"]
    assert not view.act_dupes.isEnabled() and t("gui.schema.why.dupes_array") in view.act_dupes.toolTip()
    with qtbot.assertNotEmitted(view.dupes_requested):
        view.act_dupes.trigger()
        assert not view.request_dupes()
    view.select_field("email")
    assert view.act_dupes.isEnabled() and view.act_dupes.toolTip() == t("gui.dupesdialog.action.tip")
    with qtbot.waitSignal(view.dupes_requested, timeout=1000) as sig:
        view.act_dupes.trigger()
    assert sig.args == ["email"]
    menu = view.build_menu()
    names = [a.objectName() for a in menu.actions() if a.objectName()]
    assert names.index("schema_stats") < names.index("schema_dupes") < names.index("schema_mapping")
    assert menu.actions()[-1] is view.act_mapping
    menu.deleteLater()
    # a double-click on a row: statistics of that field; it does not expand / collapse the row
    contact = view.item_for("contact")
    expanded = contact.isExpanded()
    with qtbot.waitSignal(view.stats_requested, timeout=1000) as sig:
        double_click(view, contact)
    assert sig.args == ["contact"] and contact.isExpanded() == expanded
    assert view.current_field().text == "contact"
    with qtbot.waitSignal(view.stats_requested, timeout=1000) as sig:
        double_click(view, view.item_for("contact.city"))
    assert sig.args == ["contact.city"]


def double_click(view: SchemaView, item: Any) -> None:
    """What a real double-click sends: a press / release, then the double-click (QTest.mouseDClick alone
    gives the item view no press)."""
    pos = view.tree.visualItemRect(item).center()
    vp = view.tree.viewport()
    QTest.mouseClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)
    QTest.mouseDClick(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, pos)


def test_schema_dupes_for_keys_root_and_pooled(qtbot, app, opened, tmp_path: Path, lang_uz) -> None:
    keyed = write_json(tmp_path / "k.json", {f"k{i}": {"v": i, "scores": {f"d{i * 3 + j}": j for j in range(3)}}
                                             for i in range(400)})
    s = opened(keyed)
    view = schema_view(qtbot, s)
    view.select_field("$key")
    assert view.act_dupes.isEnabled()
    with qtbot.waitSignal(view.dupes_requested, timeout=1000) as sig:
        view.act_dupes.trigger()
    assert sig.args == ["$key"]
    with qtbot.waitSignal(view.stats_requested, timeout=1000) as sig:
        view.act_stats.trigger()
    assert sig.args == ["$key"]
    view.select_field("scores.*")
    assert not view.act_dupes.isEnabled() and "(*)" in view.act_dupes.toolTip()
    assert view.act_stats.isEnabled()
    # records that are not all objects: the root row "$"
    mixed = write_json(tmp_path / "m.json", [1, {"a": 1}, "x", 2.5])
    s2 = opened(mixed)
    v2 = schema_view(qtbot, s2)
    v2.select_field("$")
    with qtbot.waitSignal(v2.dupes_requested, timeout=1000) as sig:
        v2.act_dupes.trigger()
    assert sig.args == ["$"]


# ==================================================================================
# table header "Statistika" (task 3)
# ==================================================================================
class StatsWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []

    def show_stats(self, field: Any = None, *, scope: Any = None) -> None:
        self.calls.append(field)


def header_action(view: TableView, path: str, name: str) -> QAction:
    index = next(i for i, c in enumerate(view.grid.columns()) if c.id == path)
    menu = view.header_menu(index)
    return next(a for a in menu.findChildren(QAction) if a.objectName() == name)


def test_table_header_stats_item(qtbot, opened, tmp_path: Path, lang_uz) -> None:
    s = opened(write(tmp_path / "p.json", people(20)))
    win = StatsWindow()
    qtbot.addWidget(win)
    view = TableView(s)
    win.setCentralWidget(view)
    win.resize(1000, 500)
    win.show()
    view.activate()
    view.set_columns(["id", "orders[*].sum", "$index", "$"])
    act = header_action(view, "id", "table_stats")
    assert act.text() == "Statistika" and act.isEnabled() and act.toolTip() == t("gui.statspanel.action.node.tip")
    act.trigger()
    assert win.calls == ["id"]
    header_action(view, "orders[*].sum", "table_stats").trigger()
    assert win.calls[-1] == "orders[*].sum"
    header_action(view, "$", "table_stats").trigger()
    assert win.calls[-1] == "$"
    idx = header_action(view, "$index", "table_stats")
    assert not idx.isEnabled() and idx.toolTip() == t("gui.table.menu.stats.index")
    assert not view.show_column_stats("$index") and len(win.calls) == 3
    # a view without a window offering statistics: nothing happens
    bare = TableView(s)
    qtbot.addWidget(bare)
    assert not bare.show_column_stats("id")
    bare.shutdown()
    view.shutdown()


# ==================================================================================
# "Tanlangan ikkitasini taqqoslash" (task 7)
# ==================================================================================
class CompareCalls(list):
    """The recorded ``comparewindow.compare_selected`` calls; ``real`` = the real function."""

    real: Any = None


@pytest.fixture
def compare_calls(monkeypatch):
    calls = CompareCalls()
    calls.real = comparewindow.compare_selected

    def record(window: Any, session: Any, refs: Any, **kw: Any) -> Any:
        calls.append((window, session, list(refs), kw))
        return "window"

    monkeypatch.setattr(comparewindow, "compare_selected", record)
    return calls


def tree_action(menu: Any, name: str) -> QAction:
    return next(a for a in menu.findChildren(QAction) if a.objectName() == name)


def test_tree_compare_two_selected(qtbot, app, opened, views, tmp_path: Path, compare_calls, monkeypatch,
                                   lang_uz) -> None:
    s = opened(write_json(tmp_path / "c.json", [{"id": i, "name": f"n{i}", "tags": [i]} for i in range(5)]))
    view = views(TreeView, s)
    qtbot.waitUntil(lambda: view.row_count() == 5 and all(view.cell_at(r, "key") is not None for r in range(5)),
                    timeout=TIMEOUT)
    view.grid.set_selected_ranges([(0, 1)])
    menu = view.context_menu(0)
    act = tree_action(menu, "compare_selected")
    assert act.text() == "Tanlangan ikkitasini taqqoslash" and act.isEnabled()
    act.trigger()
    window, session, refs, kw = compare_calls[-1]
    assert session is s and refs == [NodeRef(0, (), ()), NodeRef(1, (), ())] and window is view.window()
    assert "reveal" in kw
    menu.deleteLater()
    # one / three selected nodes: disabled with the reason
    for ranges in ([(2, 2)], [(0, 2)]):
        view.grid.set_selected_ranges(ranges)
        m = view.context_menu(0)
        a = tree_action(m, "compare_selected")
        assert not a.isEnabled() and a.toolTip() == t("gui.compare.menu.selected.need_two")
        m.deleteLater()
    assert view.compare_selected() is None and t("gui.compare.menu.selected.need_two") in app.statuses
    # a nested node and a record
    view.expand_node((0,))
    qtbot.waitUntil(lambda: view.row_count() == 8, timeout=TIMEOUT)
    name_row = view.row_of((0, 1))
    qtbot.waitUntil(lambda: view.cell_at(name_row, "key") is not None, timeout=TIMEOUT)
    rec4 = view.row_of((4,))
    view.grid.set_selected_ranges([(name_row, name_row), (rec4, rec4)])
    assert view.compare_refs() == [NodeRef(0, ("name",), (1,)), NodeRef(4, (), ())]
    # the real compare window opens for the two nodes
    monkeypatch.setattr(comparewindow, "compare_selected", compare_calls.real)
    win = view.compare_selected()
    assert isinstance(win, comparewindow.CompareWindow)
    assert app.tasks.wait_idle(30)
    win.close()


def test_table_compare_two_selected_cells(qtbot, app, opened, views, tmp_path: Path, compare_calls,
                                          lang_uz) -> None:
    s = opened(write_json(tmp_path / "c.json", [{"id": i, "name": f"n{i}"} for i in range(4)]))
    view = views(TableView, s)
    view.set_columns(["id", "name"])
    qtbot.waitUntil(lambda: all(view.cell_at(r, "name") is not None and view.cell_at(r, "name").text
                                for r in range(4)), timeout=TIMEOUT)
    col = next(i for i, c in enumerate(view.grid.columns()) if c.id == "name")
    view.grid.set_selected_ranges([(0, 0, col, col), (2, 2, col, col)])
    menu = view.cell_menu(0, col)
    act = tree_action(menu, "compare_selected")
    assert act.isEnabled()
    act.trigger()
    _w, session, refs, _kw = compare_calls[-1]
    assert session is s and refs == [NodeRef(0, ("name",)), NodeRef(2, ("name",))]
    menu.deleteLater()
    view.grid.set_selected_ranges([(0, 1, 0, col)])            # four cells
    assert view.compare_refs() is None
    m = view.cell_menu(0, col)
    a = tree_action(m, "compare_selected")
    assert not a.isEnabled() and a.toolTip() == t("gui.compare.menu.selected.need_two")
    m.deleteLater()
    view.grid.set_selected_ranges([])
    view.grid.set_current(1, col)                               # just the current cell
    assert view.compare_refs() is None and view.compare_selected() is None
    assert t("gui.compare.menu.selected.need_two") in app.statuses


# ==================================================================================
# gui/fields.py on the core helpers (task 4)
# ==================================================================================
def _old_is_container(fi: Any) -> bool:
    return fi.main_type in ("object", "array")


def _old_under_array(fi: Any) -> bool:
    return any(seg is Wild.ANY_INDEX for seg in fi.path)


def _old_field_paths(schema: Any, *, containers: bool = True, inside_arrays: bool = True, key_field: bool = True,
                     max_depth: int | None = None) -> list[str]:
    """The former gui/fields.py field_paths (verbatim)."""
    if schema is None:
        return []
    out: list[str] = []
    for fi in schema.fields():
        if fi.is_key:
            if key_field:
                out.append(fi.text)
            continue
        if max_depth is not None and fi.depth > max_depth:
            continue
        if not containers and _old_is_container(fi):
            continue
        if not inside_arrays and _old_under_array(fi):
            continue
        if any(seg is Wild.ANY_KEY for seg in fi.path):
            continue
        out.append(fi.text)
    return out


def _old_default_columns(schema: Any, *, limit: int = 50, max_depth: int = 3) -> list[str]:
    """The former gui/fields.py default_columns (verbatim)."""
    if schema is None:
        return ["$"]
    root = schema.root
    root_is_object = root.main_type == "object"
    cols: list[str] = []
    for fi in schema.fields():
        if len(cols) >= limit:
            break
        if fi.is_key:
            cols.append(fi.text)
            continue
        if not root_is_object or fi.depth > max_depth or _old_under_array(fi):
            continue
        if any(seg is Wild.ANY_KEY for seg in fi.path):
            continue
        if fi.main_type == "object":
            continue
        if fi.main_type == "array" and fi.depth > 1:
            continue
        cols.append(fi.text)
    if not root_is_object:
        cols.append("$")
    return cols or ["$"]


def _schema_of(tmp_path: Path, name: str, text: str, fmt: str | None = None) -> Any:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    doc = Document.open(path, format=fmt, use_cache=False)
    try:
        doc.build_index()
        return infer_schema(doc, None, sample=None)
    finally:
        doc.close()


def test_fields_helpers_are_the_core_ones_with_the_same_results(tmp_path: Path) -> None:
    assert fields.field_paths is schema_mod.field_paths and fields.default_columns is schema_mod.default_columns
    assert fields.is_container is schema_mod.is_container and fields.under_array is schema_mod.under_array
    assert (fields.DEFAULT_COLUMN_LIMIT, fields.DEFAULT_COLUMN_DEPTH, fields.CONTAINER_TYPES) == (50, 3,
                                                                                                ("object", "array"))
    deep = {"a": {"b": {"c": {"d": {"e": 1}}}}, "arr": [[1, 2], [3]], "objs": [{"x": {"y": [1, {"z": 2}]}}]}
    docs = {
        "people": (people(40), None),
        "keyed": (json.dumps({f"k{i}": {"v": i, "w": [i]} for i in range(30)}), "keyed"),
        "scalars": (json.dumps([1, "a", None, True, 2.5]), None),
        "mixed": (json.dumps([1, {"a": 1, "b": {"c": [1]}}, [1, 2]]), None),
        "maps": ("\n".join(json.dumps({"id": i, "m": {f"d{i * 3 + j}": j for j in range(3)}}) for i in range(400)),
                 "jsonl"),
        "deep": (json.dumps([deep, {"a": {"b": 2}}]), None),
        "wide": (json.dumps([{f"f{i:03d}": i for i in range(80)}]), None),
        "empty": ("[]", None),
    }
    variants = [{}, {"containers": False}, {"inside_arrays": False}, {"key_field": False}, {"max_depth": 1},
                {"max_depth": 2, "containers": False, "inside_arrays": False}]
    for name, (text, fmt) in docs.items():
        sch = _schema_of(tmp_path, f"{name}.json", text, fmt)
        for kw in variants:
            assert fields.field_paths(sch, **kw) == _old_field_paths(sch, **kw), (name, kw)
        for kw in ({}, {"limit": 5}, {"max_depth": 1}, {"limit": 3, "max_depth": 5}):
            assert fields.default_columns(sch, **kw) == _old_default_columns(sch, **kw), (name, kw)
        for fi in sch.fields():
            assert fields.is_container(fi) == _old_is_container(fi)
            assert fields.under_array(fi) == _old_under_array(fi)
    assert fields.field_paths(None) == [] and fields.default_columns(None) == ["$"]


def test_fields_store_feeds_the_shared_cache(qtbot, opened, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(30)))
    other = opened(write(tmp_path / "q.json", people(5)))
    full = infer_schema(s.doc.pinned(), None, sample=None)
    sample = infer_schema(s.doc.pinned(), None, sample=4)
    assert full.complete and not sample.complete
    assert not fields.store(s, sample, full=True)                       # not every record
    assert not fields.store(s, full, full=True, doc=other.doc)          # another document
    assert not fields.store(s, full, full=True, gen=int(s.data_gen) + 99)   # other data
    assert fields.cached_schema(s) is None
    assert fields.store(s, full, full=True, doc=s.doc.pinned())
    assert fields.cached_schema(s, full=True) is full and fields.get_schema(s) is full and not fields.is_stale(s)
    assert fields.get_schema(s, full=True) is full                       # cached: no task started
    assert not any(task.kind == "schema" for task in s.app.tasks.tasks(owner=s, include_silent=True))
    assert fields.store(s, sample, full=False)
    assert fields.cached_schema(s) is full                               # the full one stays preferred
    # after an edit: a schema of the old data is refused, one of the shown data replaces the cache
    old_gen = int(s.data_gen)
    with applied(qtbot, s):
        s.edit.set_value(NodeRef(0, ("id",)), '"changed"')
    assert int(s.data_gen) != old_gen and fields.is_stale(s)
    assert not fields.store(s, full, full=True, gen=old_gen)
    fresh = infer_schema(s.doc.pinned(), None, sample=None)
    assert fields.store(s, fresh, full=False)                            # complete: stored as full too
    assert fields.cached_schema(s, full=True) is fresh and not fields.is_stale(s)
    assert fields.schema_gen(s) == s.data_gen


# ==================================================================================
# CL-9 (task 5): the state is pinned when a value / text is asked for
# ==================================================================================
def test_cl9_value_task_reads_the_state_of_its_request(qtbot, opened, tmp_path: Path, monkeypatch) -> None:
    s = opened(write_json(tmp_path / "v.json", [{"a": "old"}, {"a": "x"}]))
    entered, release = threading.Event(), threading.Event()
    real = widgets.task_node

    def late(session: Any, ref: Any, cancel: Any = None, *, doc: Any = None) -> Any:
        entered.set()                           # the worker starts, but reads only after the edit
        assert release.wait(20)
        return real(session, ref, cancel, doc=doc)

    monkeypatch.setattr(widgets, "task_node", late)
    got: list = []
    widgets.run_value_task(s, NodeRef(0, ("a",)), lambda value, cancel: value, got.append)
    assert entered.wait(20)
    with keep_selection(s), applied(qtbot, s):
        s.edit.set_value(NodeRef(0, ("a",)), '"new"')
    release.set()
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    assert got == ["old"]                        # the state the request was made in
    assert records(s)[0]["a"] == "new"


def test_cl9_full_value_window_shows_the_state_it_was_opened_for(qtbot, opened, tmp_path: Path,
                                                                 monkeypatch) -> None:
    s = opened(write_json(tmp_path / "f.json", [{"a": "old"}]))
    entered, release = threading.Event(), threading.Event()
    real = widgets.load_full_text

    def late(*args: Any, **kw: Any) -> Any:
        entered.set()
        assert release.wait(20)
        return real(*args, **kw)

    monkeypatch.setattr(widgets, "load_full_text", late)
    dlg = FullValueDialog(s, NodeRef(0, ("a",)))
    qtbot.addWidget(dlg)
    assert entered.wait(20)
    with keep_selection(s), applied(qtbot, s):
        s.edit.set_value(NodeRef(0, ("a",)), '"new"')
    release.set()
    qtbot.waitUntil(dlg.is_loaded, timeout=TIMEOUT)
    assert dlg.text() == "old"
    dlg.close()


def test_cl9_text_build_reads_and_caches_the_state_it_was_asked_for(qtbot, opened, views, tmp_path: Path,
                                                                    monkeypatch) -> None:
    s = opened(write_json(tmp_path / "t.json", [{"a": "old"}, {"a": "x"}]))
    view = views(TextView, s)
    entered, release = threading.Event(), threading.Event()
    real = textview.build_pretty

    def late(*args: Any, **kw: Any) -> Any:
        entered.set()
        assert release.wait(20)
        return real(*args, **kw)

    monkeypatch.setattr(textview, "build_pretty", late)
    ref = NodeRef(0, ("a",))
    s.select(ref)
    assert entered.wait(20)
    key, build = view._build
    assert key[0] == 0                                  # the file's data (nothing edited yet)
    view.hide()                                         # a hidden view keeps waiting for its text
    with keep_selection(s), applied(qtbot, s):
        s.edit.set_value(ref, '"new"')
    release.set()
    qtbot.waitUntil(lambda: build.task.state != "running", timeout=TIMEOUT)
    st = textview._SHARED[s]
    cached = st.get(key)
    assert cached is not None and cached.text == '"old"'     # cached under ITS key, with ITS data
    # shown again: the new data; after an undo (the file's data again) the cached text is right
    view.show()
    qtbot.waitUntil(lambda: view.result() is not None and view.text() == '"new"', timeout=TIMEOUT)
    with keep_selection(s), applied(qtbot, s):
        s.edit.undo()
    qtbot.waitUntil(lambda: view.result() is not None and view.text() == '"old"', timeout=TIMEOUT)


def test_cl9_text_build_finished_after_its_cancel_is_not_cached(qtbot, opened, views, tmp_path: Path,
                                                               monkeypatch) -> None:
    s = opened(write_json(tmp_path / "t.json", [{"a": 1}, {"a": 2}]))
    view = views(TextView, s)
    finished = threading.Event()
    real = textview.build_pretty

    def notify(*args: Any, **kw: Any) -> Any:
        try:
            return real(*args, **kw)
        finally:
            finished.set()

    monkeypatch.setattr(textview, "build_pretty", notify)
    s.select(NodeRef(0, ()))
    key, build = view._build
    # the GUI thread processes no event while it waits: the result is not delivered yet
    assert finished.wait(20)
    build.task.cancel()                                 # cancelled after the worker finished
    qtbot.waitUntil(lambda: build.task.state != "running", timeout=TIMEOUT)
    assert build.task.state == "done"                   # it returned normally despite the cancel ...
    assert textview._SHARED[s].get(key) is None         # ... but its text is not cached
    # a normal build is cached
    s.select(NodeRef(1, ()))
    qtbot.waitUntil(lambda: view.result() is not None and '"a": 2' in view.text(), timeout=TIMEOUT)
    key2 = view._cache_key(NodeRef(1, ()), view.indent())
    assert textview._SHARED[s].get(key2) is not None
