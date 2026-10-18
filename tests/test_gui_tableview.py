"""TableView (jsonreshape/gui/tableview.py; ARCHITECTURE §11.7 "Table", AGENT_PROMPT §4.2).

Real sessions on generated files (array of nested objects with arrays, keyed, JSONL,
wrapped, single, a 200 000-record JSONL, huge records through a small
``large_node_bytes``): default columns from the schema, cell texts (scalars, containers,
wildcards, missing values, exact numbers), header sorting (single / multi key, indicators,
order checked against ``sort_records``), quick filters (typed text -> session filter, view
length checked against ``filter_records``, invalid input), the column chooser, layout
persistence, the frozen column, linking with the session (current cell, activation,
selection from another view), TSV copy, live growth, streamed huge records and "no record
is parsed on the GUI thread".
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from jsonreshape import codec  # noqa: E402
from jsonreshape.gui import copyfmt, fields, widgets  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.session import DocSession, NodeRef  # noqa: E402
from jsonreshape.gui.tableview import ROOT_COLUMN, TableView, column_spec  # noqa: E402
from jsonreshape.reader import Document  # noqa: E402
from jsonreshape.recordset import filter_records, sort_records  # noqa: E402
from jsonreshape.sortkeys import SortKey  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000


# ----------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------
@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.error_reported.connect(ctx.errors.append)
    ctx.statuses = []
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture(autouse=True)
def no_swallowed_errors(caplog):
    """The grid logs (and swallows) exceptions of ``cell()`` / ``rows_shown()``; the loader
    logs failed fetches: none may happen."""
    caplog.set_level(logging.WARNING)
    yield
    records = caplog.get_records("setup") + caplog.get_records("call")
    bad = [r for r in records if r.name.startswith("jsonreshape.gui") and r.levelno >= logging.WARNING
           and ("grid source" in r.getMessage() or "RowLoader" in r.getMessage() or r.name.endswith("tableview"))]
    assert not bad, [r.getMessage() for r in bad]


@pytest.fixture
def opened(qtbot, app):
    made: list[DocSession] = []

    def open_file(path: Path, *, wait: bool = True, **kw) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        if wait:
            with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
                s.start()
        else:
            s.start()
        return s

    yield open_file
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def make_view(qtbot, opened):
    views: list[TableView] = []

    def make(session: DocSession, *, activate: bool = True, width: int = 1400) -> TableView:
        view = TableView(session)
        qtbot.addWidget(view)
        view.resize(width, 700)
        view.show()
        if activate:
            view.activate()
        views.append(view)
        return view

    yield make
    for v in views:
        v.shutdown()


def people(n: int) -> list[dict]:
    out = []
    for i in range(n):
        rec = {
            "id": i,
            "name": f"user{i}",
            "age": (i * 7) % 90,
            "city": ["Toshkent", "Samarqand", "Buxoro"][i % 3],
            "contact": {"email": f"u{i}@x.uz", "phone": None if i % 4 == 0 else f"+998{i:07d}"},
            "orders": [{"id": i * 10 + k, "sum": k + 0.5} for k in range(i % 3)],
            "tags": ["a", "b", "c"][: i % 4],
        }
        if i % 5 == 0:
            rec["note"] = 'line1\nline2\tTab "q"'
        out.append(rec)
    return out


def write_json(path: Path, value) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def write_jsonl(path: Path, recs: list) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    return path


def col_index(view: TableView, path: str) -> int:
    return next(i for i, c in enumerate(view.grid.columns()) if c.id == path)


def wait_text(qtbot, view: TableView, row: int, path: str, expected: str) -> None:
    qtbot.waitUntil(lambda: view.cell_text(row, path) == expected, timeout=TIMEOUT)


def wait_loaded(qtbot, view: TableView, rows, path: str) -> None:
    for row in rows:
        qtbot.waitUntil(lambda r=row: view.cell_text(r, path) is not None, timeout=TIMEOUT)


def wait_idle_view(qtbot, session: DocSession) -> None:
    qtbot.waitUntil(lambda: not session.view_busy, timeout=TIMEOUT)


def view_records(session: DocSession) -> list[int]:
    return [session.record_at(i) for i in range(session.view_len())]


def wait_default_columns(qtbot, view: TableView) -> None:
    qtbot.waitUntil(lambda: view._columns_source == "default", timeout=TIMEOUT)


def header_click(qtbot, view: TableView, path: str, shift: bool = False) -> None:
    header = view.grid.header()
    rect = header.section_rect(col_index(view, path))
    assert rect.isValid()
    mods = Qt.KeyboardModifier.ShiftModifier if shift else Qt.KeyboardModifier.NoModifier
    qtbot.mouseClick(header, Qt.MouseButton.LeftButton, mods, rect.center())


def clipboard() -> str:
    return QGuiApplication.clipboard().text()


@pytest.fixture
def people_file(tmp_path: Path) -> Path:
    return write_json(tmp_path / "people.json", people(240))


# ----------------------------------------------------------------------------------
# columns and cells
# ----------------------------------------------------------------------------------
def test_default_columns_from_schema(qtbot, opened, make_view, people_file, lang_uz) -> None:
    s = opened(people_file)
    view = make_view(s, activate=False)
    assert view.visible_column_paths() == [ROOT_COLUMN]      # placeholder until the schema arrives
    view.activate()
    wait_default_columns(qtbot, view)
    schema = fields.cached_schema(s)
    assert view.visible_column_paths() == fields.default_columns(schema)
    cols = view.visible_column_paths()
    assert cols[:4] == ["id", "name", "age", "city"]
    assert "contact.email" in cols and "contact.phone" in cols and "orders" in cols and "tags" in cols
    assert "orders[*].id" not in cols and "contact" not in cols
    wait_text(qtbot, view, 0, "name", "user0")
    assert view.cell_text(0, "id") == "0"
    assert view.cell_at(0, "id").role == "number"
    wait_text(qtbot, view, 2, "orders", "[2]")
    assert view.cell_at(2, "orders").role == "array"
    assert view.cell_text(0, "contact.phone") == "null" and view.cell_at(0, "contact.phone").role == "null"
    assert view.row_label(7) == "#7"
    assert view.grid.selection_mode() == "cells"
    assert view.grid.row_count() == 240
    assert not app_errors(s)


def app_errors(session: DocSession) -> list:
    return session.app.errors


def test_cell_texts_exact_numbers_wildcards_missing(qtbot, tmp_path, opened, make_view) -> None:
    raw = ('[{"n":1e400,"m":1.10,"z":-0.0,"big":123456789012345678901234567890,"neg":-0,"s":"tab\\there",'
           '"o":{"p":1,"q":2},"a":[1,2,3],"w":[{"x":1},{"x":"t"},{"y":0}],"e":[],"u":"\\u00e9\\u2028"},'
           '{"n":true,"m":null,"s":"multi\\nline","w":[{"x":5}]},'
           '7]')
    path = tmp_path / "exact.json"
    path.write_text(raw, encoding="utf-8")
    s = opened(path)
    view = make_view(s)
    cols = ["n", "m", "z", "big", "neg", "s", "o", "a", "w[*].x", "e", "missing", "$", "$index", "u"]
    view.set_columns(cols)
    assert view.visible_column_paths() == cols
    wait_text(qtbot, view, 0, "n", "1e400")
    expect0 = {"n": ("1e400", "number"), "m": ("1.10", "number"), "z": ("-0.0", "number"),
               "big": ("123456789012345678901234567890", "number"), "neg": ("-0", "number"),
               "s": ("tab here", None), "o": ("{2}", "object"), "a": ("[3]", "array"), "w[*].x": ("1; t", None),
               "e": ("[0]", "array"), "missing": ("", None), "$": ("{11}", "object"), "$index": ("0", "number"),
               "u": ("é ", None)}
    for col, (text, role) in expect0.items():
        cell = view.cell_at(0, col)
        assert (cell.text, cell.role) == (text, role), col
    assert view.cell_at(0, "s").tooltip == "tab\there"   # the painted text differs: full text as tooltip
    wait_text(qtbot, view, 1, "n", "true")
    assert view.cell_at(1, "n").role == "bool"
    assert (view.cell_text(1, "m"), view.cell_at(1, "m").role) == ("null", "null")
    assert view.cell_text(1, "s") == "multi↵line"
    assert view.cell_text(1, "w[*].x") == "5" and view.cell_at(1, "w[*].x").role == "number"
    assert view.cell_text(1, "o") == ""
    wait_text(qtbot, view, 2, "$", "7")
    assert view.cell_text(2, "n") == ""
    # a column added later is filled at once from the kept parsed value (no refetch)
    view.add_column("w[0]")
    assert view.cell_text(0, "w[0]") == "{1}"


def test_keyed_and_jsonl_documents(qtbot, tmp_path, opened, make_view) -> None:
    keyed = write_json(tmp_path / "keyed.json", {f"k{i}": {"v": i, "name": f"n{i}"} for i in range(30)})
    s = opened(keyed)
    view = make_view(s)
    wait_default_columns(qtbot, view)
    assert view.visible_column_paths()[0] == "$key"
    assert view.grid.columns()[0].title == "Kalit" or view.grid.columns()[0].title == "Key"
    wait_text(qtbot, view, 3, "$key", "k3")
    assert view.cell_at(3, "$key").role == "key"
    assert view.cell_text(3, "v") == "3"
    assert view.row_label(3) == "#3"
    # $key / $index are sortable
    view.sort_by_column("$key", add=False)
    assert s.sort_keys == [SortKey("$key")]
    wait_idle_view(qtbot, s)
    rs = sort_records(s.doc, [SortKey("$key")])
    assert view_records(s) == [rs[i] for i in range(len(rs))] == list(range(30))   # natural order: k2 < k10
    rs.close()
    view.sort_by_column("$key")
    assert s.sort_keys == [SortKey("$key", descending=True)]
    wait_idle_view(qtbot, s)
    rs = sort_records(s.doc, [SortKey("$key", descending=True)])
    assert view_records(s) == [rs[i] for i in range(len(rs))]
    rs.close()

    jl = write_jsonl(tmp_path / "p.jsonl", people(20))
    s2 = opened(jl)
    view2 = make_view(s2)
    wait_default_columns(qtbot, view2)
    wait_text(qtbot, view2, 19, "name", "user19")
    assert view2.cell_text(19, "tags") == "[3]"


def test_single_and_wrapped_documents(qtbot, tmp_path, opened, make_view) -> None:
    single = write_json(tmp_path / "single.json", {"a": 1, "b": [1, 2]})
    s = opened(single)
    view = make_view(s)
    view.set_columns(["a", "b", "$"])
    wait_text(qtbot, view, 0, "b", "[2]")
    assert view.grid.row_count() == 1
    assert view.cell_text(0, "$") == "{2}"
    assert not view.sort_by_column("a")                 # single documents: no sorting / filtering
    assert s.sort_keys == []
    assert not view.filter_editor("a").isEnabled()
    assert not view.apply_quick_filter("a", "=1") and s.quick_filters == {}
    view.grid.set_current(0, col_index(view, "b"))
    qtbot.waitUntil(lambda: s.current == NodeRef(None, ("b",)), timeout=TIMEOUT)

    wrapped = write_json(tmp_path / "wrapped.json", {"meta": {"v": 1}, "data": people(12)})
    s2 = opened(wrapped)
    view2 = make_view(s2)
    wait_default_columns(qtbot, view2)
    wait_text(qtbot, view2, 11, "name", "user11")
    view2.grid.set_current(11, col_index(view2, "name"))
    qtbot.waitUntil(lambda: s2.current == NodeRef(11, ("name",)), timeout=TIMEOUT)


# ----------------------------------------------------------------------------------
# sorting
# ----------------------------------------------------------------------------------
def test_header_sort_single_and_multi(qtbot, opened, make_view, people_file) -> None:
    s = opened(people_file)
    view = make_view(s)
    view.set_columns(["id", "age", "city", "name"])
    header_click(qtbot, view, "age")
    assert s.sort_keys == [SortKey("age")]
    wait_idle_view(qtbot, s)
    rs = sort_records(s.doc, [SortKey("age")])
    assert view_records(s) == [rs[i] for i in range(len(rs))]
    rs.close()
    age = view.grid.columns()[col_index(view, "age")]
    assert (age.sort_order, age.sort_priority) == (1, 1)
    wait_text(qtbot, view, 0, "age", "0")

    header_click(qtbot, view, "age")
    assert s.sort_keys == [SortKey("age", descending=True)]
    wait_idle_view(qtbot, s)
    assert view.grid.columns()[col_index(view, "age")].sort_order == -1

    header_click(qtbot, view, "city", shift=True)        # secondary key
    keys = [SortKey("age", descending=True), SortKey("city")]
    assert s.sort_keys == keys
    wait_idle_view(qtbot, s)
    rs = sort_records(s.doc, keys)
    assert view_records(s) == [rs[i] for i in range(len(rs))]
    rs.close()
    cols = view.grid.columns()
    assert (cols[col_index(view, "age")].sort_priority, cols[col_index(view, "city")].sort_priority) == (1, 2)
    assert cols[col_index(view, "city")].sort_order == 1

    header_click(qtbot, view, "city", shift=True)        # toggles only that key
    assert s.sort_keys == [SortKey("age", descending=True), SortKey("city", descending=True)]
    header_click(qtbot, view, "city", shift=True)        # third shift-click removes it
    assert s.sort_keys == [SortKey("age", descending=True)]
    header_click(qtbot, view, "age")                     # third plain click: unsorted
    assert s.sort_keys == []
    wait_idle_view(qtbot, s)
    assert view_records(s) == list(range(240))
    assert all(c.sort_order == 0 for c in view.grid.columns())

    # the current record stays selected when the order changes
    view.grid.set_current(5, col_index(view, "name"))
    header_click(qtbot, view, "city")
    wait_idle_view(qtbot, s)
    qtbot.waitUntil(lambda: view.record_at_row(view.grid.current()[0]) == 5, timeout=TIMEOUT)
    assert view.grid.current()[0] == view_records(s).index(5)


def test_unsortable_column_and_header_menu(qtbot, opened, make_view, people_file, lang_uz) -> None:
    s = opened(people_file)
    view = make_view(s)
    view.set_columns(["id", "**.sum", "name"])
    assert not view.sort_by_column("**.sum")
    assert s.sort_keys == []
    assert view.grid.columns()[1].sortable is False
    menu = view.header_menu(1)
    acts = {a.objectName(): a for a in menu.findChildren(type(menu.actions()[0]))}
    assert not acts["table_sort_asc"].isEnabled() and acts["table_sort_asc"].toolTip()
    menu = view.header_menu(0)
    acts = {a.objectName(): a for a in menu.findChildren(type(menu.actions()[0]))}
    acts["table_sort_desc"].trigger()
    assert s.sort_keys == [SortKey("id", descending=True)]
    wait_idle_view(qtbot, s)
    assert view_records(s)[0] == 239
    menu = view.header_menu(0)
    acts = {a.objectName(): a for a in menu.findChildren(type(menu.actions()[0]))}
    acts["table_freeze"].trigger()
    assert view.grid.frozen_columns() == 1 and view.is_frozen()
    assert view.filter_editor("id").parentWidget() is view.strip       # stays in place while scrolling
    assert view.filter_editor("name").parentWidget() is view.strip.clip
    acts["table_hide_column"].trigger()
    assert view.visible_column_paths() == ["**.sum", "name"]
    menu = view.header_menu(-1)
    sub = next(m for m in menu.findChildren(type(menu)) if m.objectName() == "table_show_hidden")
    assert sub.isEnabled() and len(sub.actions()) == 1
    sub.actions()[0].trigger()
    assert view.visible_column_paths() == ["id", "**.sum", "name"]


# ----------------------------------------------------------------------------------
# quick filters
# ----------------------------------------------------------------------------------
def reference_count(session: DocSession, expr: str) -> int:
    rs = filter_records(session.doc, expr)
    try:
        return len(rs)
    finally:
        rs.close()


def test_quick_filter_typing_and_invalid_input(qtbot, opened, make_view, people_file, lang_uz) -> None:
    s = opened(people_file)
    view = make_view(s)
    view.set_columns(["id", "age", "city", "contact.phone", "tags"])
    ed = view.filter_editor("city")
    assert ed is not None and view.filter_row_visible()
    qtbot.keyClicks(ed, "SAMAR")
    expr = 'lower(string(city)) contains "samar"'
    qtbot.waitUntil(lambda: s.quick_filters.get("city") == expr, timeout=TIMEOUT)
    wait_idle_view(qtbot, s)
    assert s.view_len() == reference_count(s, expr) == 80
    assert view.grid.row_count() == 80
    wait_text(qtbot, view, 0, "city", "Samarqand")

    age = view.filter_editor("age")
    qtbot.keyClicks(age, ">abc")
    qtbot.waitUntil(lambda: age.error is not None, timeout=TIMEOUT)
    assert age.property("invalid") is True and "abc" in age.toolTip()
    assert "age" not in s.quick_filters and s.view_len() == 80
    age.setText("")
    qtbot.keyClicks(age, ">=60")
    qtbot.keyClick(age, Qt.Key.Key_Return)                # Enter applies at once
    assert s.quick_filters["age"] == "age >= 60" and age.error is None
    wait_idle_view(qtbot, s)
    both = f"({expr}) and (age >= 60)"
    assert s.view_len() == reference_count(s, both)
    assert view.clear_button.isEnabled()

    phone = view.filter_editor("contact.phone")
    phone.setText("null")
    phone.commit()
    wait_idle_view(qtbot, s)
    assert s.view_len() == reference_count(s, f"{both} and (contact.phone is null)")

    tags = view.filter_editor("tags")
    tags.setText("2..3")      # a number range on an array: no element in it is a number between -> 0
    tags.commit()
    wait_idle_view(qtbot, s)
    assert s.quick_filters["tags"] == "tags between 2 and 3"

    # a re-created view shows the typed texts
    view.shutdown()
    view2 = make_view(s)
    assert view2.visible_column_paths() == ["id", "age", "city", "contact.phone", "tags"]   # remembered layout
    assert view2.filter_editor("city").text() == "SAMAR"
    assert view2.filter_editor("age").text() == ">=60"
    view2.clear_quick_filters()
    assert s.quick_filters == {}
    wait_idle_view(qtbot, s)
    assert s.view_len() == 240
    assert all(not view2.filter_editor(p).text() for p in view2.visible_column_paths())
    assert not view2.clear_button.isEnabled()


def test_quick_filter_escape_and_external_reset(qtbot, opened, make_view, people_file) -> None:
    s = opened(people_file)
    view = make_view(s)
    view.set_columns(["id", "name"])
    ed = view.filter_editor("name")
    qtbot.keyClicks(ed, "user1")
    qtbot.keyClick(ed, Qt.Key.Key_Return)
    wait_idle_view(qtbot, s)
    assert s.view_len() == reference_count(s, 'lower(string(name)) contains "user1"')
    qtbot.keyClick(ed, Qt.Key.Key_Escape)                 # Esc clears and applies
    assert ed.text() == "" and s.quick_filters == {}
    view.apply_quick_filter("id", "<10")
    wait_idle_view(qtbot, s)
    assert view.filter_editor("id").text() == "<10"
    s.reset_view()                                         # cleared elsewhere -> the editor follows
    assert view.filter_editor("id").text() == ""
    # Alt+Down in the grid shows the row and focuses the current column's editor (Ctrl+F is
    # the quick find bar since the fix round: the filter row keeps its own key and button)
    view.set_filter_row_visible(False)
    assert view.grid.header_extra_height() == 0
    view.grid.set_current(0, 1)
    view.activateWindow()
    view.grid.setFocus()
    qtbot.keyClick(view.grid, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    assert view.filter_row_visible() and view.grid.header_extra_height() > 0
    qtbot.waitUntil(lambda: view.filter_editor("name").hasFocus(), timeout=5000)
    assert view.grid.current() == (0, 1)                   # Alt+Down did not move the current cell


# ----------------------------------------------------------------------------------
# column chooser / layout
# ----------------------------------------------------------------------------------
def test_column_chooser_add_remove_reorder_custom(qtbot, opened, make_view, people_file, lang_uz) -> None:
    s = opened(people_file)
    view = make_view(s)
    wait_default_columns(qtbot, view)
    defaults = view.visible_column_paths()
    dlg = view.open_column_chooser()
    paths = dlg.paths()
    assert paths[:len(defaults)] == defaults
    assert "orders[*].sum" in paths and "contact" in paths      # every schema field is offered
    assert dlg.columns() == defaults
    assert "%" in dlg.find("contact.phone").text()              # type / presence hint
    assert dlg.set_checked("name", False)
    assert not dlg.add_custom("a[")                               # invalid path
    assert dlg.error_label.isVisibleTo(dlg) and dlg.error_label.text()
    assert dlg.add_custom("orders[*].sum")
    assert not dlg.error_label.isVisibleTo(dlg)
    assert dlg.add_custom("$index")
    dlg.list.setCurrentRow(dlg.paths().index("$index"))
    for _ in range(len(dlg.paths())):
        dlg.move_current(-1)
    assert dlg.paths()[0] == "$index"
    dlg.search.setText("orders")
    assert all(item.isHidden() == ("orders" not in item.data(Qt.ItemDataRole.UserRole)) for item in dlg.items())
    dlg.search.setText("")
    expected = dlg.columns()
    assert expected[0] == "$index" and "name" not in expected and "orders[*].sum" in expected
    dlg.accept()
    assert view.visible_column_paths() == expected
    wait_text(qtbot, view, 2, "orders[*].sum", "0.5; 1.5")
    wait_text(qtbot, view, 2, "$index", "2")

    dlg2 = view.open_column_chooser()
    dlg2.reset_defaults()
    assert dlg2.columns() == defaults
    dlg2.reject()
    assert view.visible_column_paths() == expected              # cancelled: unchanged
    dlg3 = view.open_column_chooser()
    dlg3.reset_defaults()
    dlg3.accept()
    assert view.visible_column_paths() == defaults


def test_layout_persisted_and_restored(qtbot, app, opened, make_view, people_file, tmp_path) -> None:
    s = opened(people_file)
    view = make_view(s)
    view.set_columns(["name", "id", "age", "city", "tags"])
    view.grid.set_column_width(col_index(view, "age"), 222)
    view.grid.move_column(col_index(view, "id"), 0)              # drag-reorder
    view.hide_column("city")
    view.set_frozen(True)
    view.set_filter_row_visible(False)
    expected = view.layout_state()
    assert [c["path"] for c in expected["columns"]] == ["id", "name", "age", "city", "tags"]
    view.shutdown()
    s.close()
    stored = app.state.table_layout(people_file)
    assert stored == expected
    app.state.save()
    assert GuiState.load(tmp_path / "gui_state.json").table_layout(people_file) == expected

    s2 = opened(people_file)
    view2 = make_view(s2)
    cols = view2.grid.columns()
    assert [c.id for c in cols] == ["id", "name", "age", "city", "tags"]
    assert cols[2].width == 222 and not cols[3].visible
    assert view2.visible_column_paths() == ["id", "name", "age", "tags"]
    assert view2.is_frozen() and not view2.filter_row_visible()
    assert view2._columns_source == "layout"
    wait_text(qtbot, view2, 4, "name", "user4")
    # unknown paths are allowed: empty cells
    view2.add_column("no.such.field")
    wait_text(qtbot, view2, 4, "no.such.field", "")
    view2.reset_columns()
    assert app.state.table_layout(people_file) is None
    wait_default_columns(qtbot, view2)
    assert not view2.is_frozen()


# ----------------------------------------------------------------------------------
# linking
# ----------------------------------------------------------------------------------
def test_current_cell_selects_and_activation_reveals(qtbot, opened, make_view, people_file) -> None:
    s = opened(people_file)
    view = make_view(s)
    view.set_columns(["id", "orders", "contact.email", "orders[*].id", "note"])
    wait_text(qtbot, view, 2, "orders", "[2]")
    view.grid.setFocus()
    view.grid.set_current(4, col_index(view, "contact.email"))
    qtbot.waitUntil(lambda: s.current == NodeRef(4, ("contact", "email")), timeout=TIMEOUT)
    view.grid.set_current(2, col_index(view, "orders[*].id"))     # several values: their common parent
    qtbot.waitUntil(lambda: s.current == NodeRef(2, ("orders",)), timeout=TIMEOUT)
    view.grid.set_current(1, col_index(view, "note"))             # missing: the record itself
    qtbot.waitUntil(lambda: s.current == NodeRef(1, ()), timeout=TIMEOUT)

    revealed: list = []
    s.reveal_requested.connect(revealed.append)
    view.grid.set_current(2, col_index(view, "orders"))
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT):
        qtbot.keyClick(view.grid, Qt.Key.Key_Return)              # activation of a container cell
    assert s.current == NodeRef(2, ("orders",))
    assert revealed[-1] == NodeRef(2, ("orders",))
    n = len(revealed)
    view.grid.set_current(2, col_index(view, "id"))
    view.grid.activated.emit(2, col_index(view, "id"))            # a scalar: no reveal
    qtbot.wait(50)
    assert len(revealed) == n


def test_follows_selection_from_other_views(qtbot, opened, make_view, people_file) -> None:
    s = opened(people_file)
    view = make_view(s)
    view.set_columns(["id", "city", "contact.email", "orders"])
    view.apply_quick_filter("city", "=Buxoro")
    wait_idle_view(qtbot, s)
    recs = view_records(s)
    assert recs[:3] == [2, 5, 8]
    other = object()
    s.select(NodeRef(8, ("contact", "email")), origin=other)
    qtbot.waitUntil(lambda: view.grid.current() == (2, col_index(view, "contact.email")), timeout=TIMEOUT)
    s.select(NodeRef(200, ("orders", 1, "sum")), origin=other)   # the column leading to the node
    pos = recs.index(200)
    qtbot.waitUntil(lambda: view.grid.current() == (pos, col_index(view, "orders")), timeout=TIMEOUT)
    first, last = view.grid.visible_rows()
    assert first <= pos <= last
    qtbot.wait(SELECT_WAIT)
    assert s.current == NodeRef(200, ("orders", 1, "sum"))       # following never echoes a select back
    s.select(NodeRef(1, ("id",)), origin=other)                  # not in the view: nothing moves
    qtbot.wait(100)
    assert view.grid.current() == (pos, col_index(view, "orders"))


SELECT_WAIT = 300


# ----------------------------------------------------------------------------------
# copy
# ----------------------------------------------------------------------------------
def test_tsv_copy(qtbot, app, opened, make_view, people_file) -> None:
    s = opened(people_file)
    view = make_view(s)
    view.set_columns(["id", "note", "tags", "missing"])
    wait_loaded(qtbot, view, range(6), "note")
    grid = view.grid
    grid.setFocus()
    grid.set_current(0, 0)
    for _ in range(5):
        qtbot.keyClick(grid, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    for _ in range(3):
        qtbot.keyClick(grid, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    assert grid.selected_ranges() == [(0, 5, 0, 3)]
    assert view.selection_count() == 6
    note = 'line1\nline2\tTab "q"'
    rows = [[str(i), note if i % 5 == 0 else "", json.dumps(["a", "b", "c"][: i % 4]).replace(" ", ""), ""]
            for i in range(6)]
    text = view.copy_value("json")                                # the window's Ctrl+C
    assert text == copyfmt.tsv_text(rows) == clipboard()
    assert '"line1\nline2\tTab ""q"""' in text                    # quoted for Excel
    view.set_copy_headers(True)
    text = view.copy_selection()
    assert text.split("\n")[0] == "id\tnote\ttags\tmissing"
    assert app.state.get("table_copy_headers") is True
    view.set_copy_headers(False)
    qtbot.keyClick(grid, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)   # without a main window
    assert clipboard() == copyfmt.tsv_text(rows)

    # one cell: its full text (copy cell) / its node's value (copy value)
    grid.set_current(0, col_index(view, "note"))
    assert view.copy_cell() == note == clipboard()
    task = view.copy_value("json")
    qtbot.waitUntil(lambda: clipboard() == json.dumps(note), timeout=TIMEOUT)
    assert task is not None
    grid.set_current(1, col_index(view, "missing"))
    assert view.copy_value("json") is None                        # nothing to copy

    # the cell menu
    menu = view.cell_menu(3, col_index(view, "tags"))
    names = {a.objectName() for a in menu.actions()}
    assert {"table_copy_cell", "table_copy_tsv", "table_copy_headers", "filter_by_value", "show_in_tree",
            "full_value"} <= names
    grid.set_current(3, col_index(view, "tags"))
    next(a for a in menu.actions() if a.objectName() == "table_copy_cell").trigger()
    assert clipboard() == '["a","b","c"]'


def test_tsv_copy_loads_missing_rows_in_a_task(qtbot, app, opened, make_view, tmp_path) -> None:
    path = write_jsonl(tmp_path / "many.jsonl", [{"id": i, "v": f"x{i}", "a": [i]} for i in range(3000)])
    s = opened(path)
    view = make_view(s)
    view.set_columns(["id", "v", "a"])
    wait_text(qtbot, view, 0, "v", "x0")
    view.grid.setFocus()
    view.grid.select_all()
    assert view.selection_count() == 3000
    task = view.copy_selection()
    assert not isinstance(task, str)                              # rows not loaded: a task
    expected = copyfmt.tsv_text([[str(i), f"x{i}", f"[{i}]"] for i in range(3000)])
    qtbot.waitUntil(lambda: clipboard() == expected, timeout=TIMEOUT)
    # the confirmation above COPY_CONFIRM_ROWS
    import jsonreshape.gui.tableview as tv

    asked: list = []
    widgets.confirm_handler = lambda parent, text, title: asked.append(text) or False
    try:
        old = tv.COPY_CONFIRM_ROWS
        tv.COPY_CONFIRM_ROWS = 100
        assert view.copy_selection() is None
        assert asked and ("3 000" in asked[0] or "3,000" in asked[0])
    finally:
        tv.COPY_CONFIRM_ROWS = old
        widgets.confirm_handler = None


# ----------------------------------------------------------------------------------
# big files, huge records, threads
# ----------------------------------------------------------------------------------
def test_live_growth_and_scrolling_200k(qtbot, app, opened, make_view, tmp_path) -> None:
    n = 200_000
    path = tmp_path / "big.jsonl"
    path.write_text("".join(f'{{"id":{i},"v":"r{i}"}}\n' for i in range(n)), encoding="utf-8")
    s = opened(path, wait=False)
    view = make_view(s)
    view.set_columns(["id", "v"])
    seen: list[tuple[int, int]] = []
    s.count_changed.connect(lambda: seen.append((view.grid.row_count(), s.view_len())))
    qtbot.waitUntil(lambda: s.state == "ready", timeout=TIMEOUT)
    qtbot.waitUntil(lambda: view.grid.row_count() == n, timeout=TIMEOUT)
    assert seen and all(a == b for a, b in seen)                  # the grid follows every count change
    view.grid.scroll_to(n - 1, "bottom")
    wait_text(qtbot, view, n - 1, "v", f"r{n - 1}")
    assert view.row_label(n - 1) == f"#{n - 1}"
    view.grid.scroll_to(123_456, "top")
    wait_text(qtbot, view, 123_456, "id", "123456")
    # a filter: partial results become visible, the grid follows the growing view
    view.apply_quick_filter("id", "<1000")
    wait_idle_view(qtbot, s)
    assert view.grid.row_count() == s.view_len() == 1000


def test_huge_records_are_streamed(qtbot, app, opened, make_view, tmp_path) -> None:
    app.settings.large_node_bytes = 4096
    big = list(range(1500))
    recs = [
        {"id": 0, "name": "small", "big": [1, 2], "obj": {"a": 1}},
        {"id": 1, "name": "huge", "big": big, "obj": {"a": 1, "b": {"c": 2}}, "s": "x" * 9000,
         "w": [{"x": 1}, {"x": "t"}]},
    ]
    body = ",".join(json.dumps(r) for r in recs)
    dup = '{"name":"first","pad":' + json.dumps(big) + ',"name":"last","id":2}'    # duplicate key
    path = tmp_path / "huge.json"
    path.write_text("[" + body + "," + dup + "]", encoding="utf-8")
    s = opened(path)
    assert s.doc.is_huge(1) and s.doc.is_huge(2) and not s.doc.is_huge(0)
    loads_sizes: list[int] = []
    orig = codec.loads

    def spy(data, *a, **k):
        if isinstance(data, (bytes, bytearray, memoryview)):
            loads_sizes.append(len(data))
        return orig(data, *a, **k)

    codec.loads = spy
    try:
        view = make_view(s)
        view.set_columns(["id", "name", "big", "obj", "s", "w[*].x", "$", "obj.b.c"])
        wait_text(qtbot, view, 1, "big", "[1500]")
        assert view.cell_at(1, "big").role == "array"
        assert view.cell_text(1, "name") == "huge"
        assert view.cell_text(1, "obj") == "{2}"
        assert view.cell_text(1, "s") == "x" * 999 + "…"            # painted text is capped
        assert view.cell_at(1, "s").tooltip.startswith("xxx")
        assert view.cell_text(1, "w[*].x") == "1; t"
        assert view.cell_text(1, "$") == "{6}"
        assert view.cell_text(1, "obj.b.c") == "2"
        wait_text(qtbot, view, 2, "name", "last")                    # dict semantics via stream_values
        assert view.cell_text(2, "$") == "{3}" and view.cell_text(2, "pad") is None
        assert view.cell_text(0, "big") == "[2]"
        assert all(size < 4096 for size in loads_sizes)               # huge records were never parsed whole
        # a new column on a streamed row is fetched again
        view.add_column("pad")
        wait_text(qtbot, view, 2, "pad", "[1500]")
        wait_text(qtbot, view, 1, "pad", "")
        # activation of a streamed container reveals its path
        view.grid.set_current(1, col_index(view, "big"))
        with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as blocker:
            view.grid.activated.emit(1, col_index(view, "big"))
        assert blocker.args[0] == NodeRef(1, ("big",))
        # copying a streamed container gives its size text
        view.grid.set_current(1, col_index(view, "big"))
        view.grid.select_all()
        task = view.copy_selection()
        qtbot.waitUntil(lambda: "[1500]" in clipboard(), timeout=TIMEOUT)
        assert task is not None
    finally:
        codec.loads = orig


def test_no_record_parsing_on_the_gui_thread(qtbot, app, opened, make_view, people_file, monkeypatch) -> None:
    main = threading.current_thread()
    gui_parses: list[str] = []
    orig_loads = codec.loads
    orig_rb = Document.record_bytes

    def spy_loads(data, *a, **k):
        if isinstance(data, (bytes, bytearray, memoryview)) and threading.current_thread() is main:
            gui_parses.append("loads")
        return orig_loads(data, *a, **k)

    def spy_rb(self, i):
        if threading.current_thread() is main:
            gui_parses.append(f"record_bytes({i})")
        return orig_rb(self, i)

    monkeypatch.setattr(codec, "loads", spy_loads)
    monkeypatch.setattr(Document, "record_bytes", spy_rb)
    s = opened(people_file)
    view = make_view(s)
    wait_default_columns(qtbot, view)
    wait_text(qtbot, view, 3, "name", "user3")
    view.grid.scroll_to(200, "top")
    wait_text(qtbot, view, 200, "name", "user200")
    header_click(qtbot, view, "age")
    wait_idle_view(qtbot, s)
    view.apply_quick_filter("city", "toshkent")
    wait_idle_view(qtbot, s)
    wait_loaded(qtbot, view, range(5), "name")
    view.grid.set_current(2, col_index(view, "orders"))
    view.grid.activated.emit(2, col_index(view, "orders"))
    s.select(NodeRef(9, ("tags",)), origin=object())
    qtbot.waitUntil(lambda: view.record_at_row(view.grid.current()[0]) == 9, timeout=TIMEOUT)
    view.add_column("orders[*].sum")
    view.grid.select_all()
    view.copy_selection()
    qtbot.wait(100)
    assert gui_parses == []


def test_filter_row_follows_column_geometry(qtbot, opened, make_view, people_file) -> None:
    s = opened(people_file)
    view = make_view(s, width=700)
    paths = ["id", "name", "age", "city", "contact.email", "contact.phone", "tags", "orders"]
    view.set_columns(paths)
    wait_text(qtbot, view, 0, "name", "user0")
    grid, strip, header = view.grid, view.strip, view.grid.header()

    def check() -> None:
        for i, path in enumerate(paths):
            ed = view.filter_editor(path)
            col = grid.columns()[i]
            if not col.visible:
                assert ed.isHidden()
                continue
            rect = header.section_rect(i)
            x = ed.mapTo(strip, QPoint(0, 0)).x()
            assert (x, ed.width()) == (rect.x(), rect.width() - 1), path
        assert strip.geometry().x() == header.geometry().x()
        assert strip.geometry().top() == header.geometry().bottom() + 1     # right under the header
        assert grid.viewport().geometry().top() == strip.geometry().bottom() + 1

    check()
    grid.set_column_width(1, 260)                        # resize
    check()
    grid.move_column(0, 3)                               # reorder
    paths[:] = [c.id for c in grid.columns()]
    check()
    grid.set_horizontal_offset(300)                      # horizontal scroll
    assert grid.horizontal_offset() == 300
    check()
    view.set_frozen(True)                                # frozen first column stays at the gutter
    first = paths[0]
    assert view.filter_editor(first).mapTo(strip, QPoint(0, 0)).x() == grid.gutter_width()
    grid.set_horizontal_offset(150)
    check()
    view.hide_column("age")
    check()
    grid.zoom(3)                                         # bigger font: gutter / rows change
    check()
    grid.viewport().grab()                               # paints every visible cell (frozen column included)
    view.set_filter_row_visible(False)
    assert strip.isHidden() and grid.viewport().geometry().top() == header.geometry().bottom() + 1


def test_in_a_document_tab_with_the_real_views(qtbot, app, opened, people_file) -> None:
    from jsonreshape.gui.doctab import DocumentTab, default_view_registry

    s = opened(people_file)
    registry = default_view_registry()
    # since R2-22 the views not shown by a new tab are imported on first use (LazyView)
    entry = registry["table"]
    assert (entry.resolve() if hasattr(entry, "resolve") else entry) is TableView
    tab = DocumentTab(s, view_registry=registry)
    qtbot.addWidget(tab)
    tab.resize(1600, 800)
    tab.show()
    try:
        view = tab.set_active_view_kind("table")
        assert isinstance(view, TableView) and tab.active_view() is view
        wait_default_columns(qtbot, view)
        assert tab.table_view() is view
        added = tab.add_table_column("orders[*].sum")          # the schema view's "add as column"
        assert added is view and view.visible_column_paths()[-1] == "orders[*].sum"
        wait_text(qtbot, view, 2, "orders[*].sum", "0.5; 1.5")
        counts: list = []
        tab.selection_changed.connect(lambda: counts.append(view.selection_count()))
        view.grid.set_current(0, 0)
        view.grid.select_all()
        assert counts and counts[-1] == 240
        view.grid.set_current(4, col_index(view, "name"))
        assert view.selected_text() == "user4"
        view.copy_value("json")                                  # what the window's Ctrl+C calls
        qtbot.waitUntil(lambda: clipboard() == '"user4"', timeout=TIMEOUT)
        view.quick_find()                                        # the window's Ctrl+F
        assert view.filter_row_visible()
    finally:
        tab.shutdown()


def test_column_spec_and_busy_indicator(qtbot, opened, make_view, people_file) -> None:
    assert column_spec("$key").kind == "key" and column_spec("$index").kind == "index"
    assert column_spec("$").pattern.segments == ()
    bad = column_spec("a[")
    assert bad.error and bad.sort_block()
    s = opened(people_file)
    view = make_view(s)
    view.set_columns(["id", "a["])
    wait_text(qtbot, view, 0, "a[", "")
    seen: list[bool] = []
    s.view_busy_changed.connect(lambda busy: seen.append((busy, not view.busy_label.isHidden())))
    view.sort_by_column("id")
    wait_idle_view(qtbot, s)
    assert (True, True) in seen and seen[-1] == (False, False)
