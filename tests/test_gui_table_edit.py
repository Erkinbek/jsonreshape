"""Editing in the table view (jsonreshape/gui/tableview.py; ARCHITECTURE §12.9 "Table").

Real sessions with a real :class:`~jsonreshape.gui.editing.EditController`: inline editing of strings and numbers
(exact tokens, Unicode), the bool toggle, the JSON dialog (Shift+Enter; null), ``$key`` renames in keyed and wrapped
object documents, ``$index`` read-only, missing fields created with ``set_path``, wildcard cells (one match editable,
several refused), Del on fields (one step, undo / redo), record delete / insert / duplicate / paste / move, every
document kind, filtered and sorted views after inserts and deletes, loader refresh after undo / redo, editors closed
when their record changes, big record selections through ``delete_records`` / ``duplicate_records``, and no data read
above 64 KiB in the GUI thread.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from jsonreshape.gui import editors, tableview, widgets  # noqa: E402
from jsonreshape.gui.session import NodeRef  # noqa: E402
from jsonreshape.gui.tableview import TableView  # noqa: E402
from jsonreshape.sortkeys import SortKey  # noqa: E402

from views_edit_helpers import (  # noqa: E402,F401
    TIMEOUT,
    GuiReads,
    app,
    applied,
    no_logged_errors,
    opened,
    record_text,
    records,
    views,
    write,
    write_json,
)

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("no_logged_errors")]


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
PEOPLE = [
    {"id": 0, "name": "Ali", "price": 1.10, "ok": True, "note": None, "tags": ["a"], "contact": {"city": "Toshkent"}},
    {"id": 1, "name": "Vali", "price": 12345678901234567890, "ok": False, "tags": ["b", "c"]},
    {"id": 2, "name": "G'ani", "price": -0.0, "ok": True, "note": "bor", "tags": [], "contact": {}},
    {"id": 3, "name": "Тошкент", "price": 0.1, "ok": False, "note": "yo'q", "tags": ["d"],
     "contact": {"city": "Buxoro"}},
    {"id": 4, "name": "Olim", "price": 7, "ok": True, "tags": ["e", "f", "g"]},
    {"id": 5, "name": "Sobir", "price": 3, "ok": True, "tags": ["h"]},
]


def people_file(tmp_path: Path, name: str = "people.json") -> Path:
    text = json.dumps(PEOPLE, ensure_ascii=False, indent=2).replace('"price": 1.1,', '"price": 1.10,')
    return write(tmp_path / name, text)


def col(view: TableView, path: str) -> int:
    return next(i for i, c in enumerate(view.grid.columns()) if c.id == path)


def wait_text(qtbot, view: TableView, row: int, path: str, text: str) -> None:
    qtbot.waitUntil(lambda: view.cell_text(row, path) == text, timeout=TIMEOUT)


def wait_loaded(qtbot, view: TableView, rows, path: str) -> None:
    qtbot.waitUntil(lambda: all(view.cell_text(r, path) is not None for r in rows), timeout=TIMEOUT)


def table(qtbot, views, s, columns: list[str]) -> TableView:
    view = views(TableView, s)
    view.set_columns(columns)
    wait_loaded(qtbot, view, range(min(3, s.view_len())), columns[0])
    return view


def current(view: TableView, row: int, path: str) -> None:
    view.grid.setFocus()
    view.grid.set_current(row, col(view, path))


def inline(qtbot, view: TableView) -> editors.InlineEditor:
    qtbot.waitUntil(lambda: view.inline_editor() is not None, timeout=TIMEOUT)
    return view.inline_editor()


def dialog(qtbot, view: TableView):
    qtbot.waitUntil(lambda: view.edit_dialog() is not None, timeout=TIMEOUT)
    dlg = view.edit_dialog()
    qtbot.waitUntil(lambda: not getattr(dlg, "loading", False), timeout=TIMEOUT)
    return dlg


def commit_inline(qtbot, s, view: TableView, text: str) -> list:
    ed = inline(qtbot, view)
    with applied(qtbot, s) as got:
        ed.setText(text)
        # a new record key is looked up first (an existing one is refused in the editor, review3 L1):
        # its commit follows the answer
        assert ed.commit() or ed.checking
    assert ed.done
    return got


# ----------------------------------------------------------------------------------
# inline editing of scalars
# ----------------------------------------------------------------------------------
def test_inline_string_edit_keeps_unicode(qtbot, opened, views, tmp_path) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "name", "price"])
    current(view, 1, "name")
    view.edit_cell()
    ed = inline(qtbot, view)
    assert ed.kind == "string" and ed.text() == "Vali"
    got = commit_inline(qtbot, s, view, "O'zbekiston — Тошкент 😀 \"q\"")
    assert got[0].records == frozenset({1})
    wait_text(qtbot, view, 1, "name", "O'zbekiston — Тошкент 😀 \"q\"")
    assert records(s)[1]["name"] == "O'zbekiston — Тошкент 😀 \"q\""
    assert s.edit.dirty and s.edit.can_undo
    # the other rows were not touched
    assert view.cell_text(0, "name") == "Ali" and view.cell_text(2, "name") == "G'ani"


@pytest.mark.parametrize("token", ["12345678901234567890", "1e400", "-0.0", "2.50", "0.1"])
def test_inline_number_edit_keeps_the_exact_token(qtbot, opened, views, tmp_path, token) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "price"])
    current(view, 0, "price")
    view.edit_cell()
    ed = inline(qtbot, view)
    assert ed.kind == "number" and ed.text() == "1.10"       # the exact token, not 1.1
    commit_inline(qtbot, s, view, token)
    wait_text(qtbot, view, 0, "price", token)
    assert f'"price": {token}' in record_text(s, 0)


def test_enter_and_double_click(qtbot, opened, views, tmp_path) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "name", "tags", "contact"])
    current(view, 2, "name")
    qtbot.keyClick(view.grid, Qt.Key.Key_Return)                 # Enter on a scalar: edit
    ed = inline(qtbot, view)
    assert ed.text() == "G'ani"
    ed.cancel()
    qtbot.waitUntil(lambda: view.inline_editor() is None, timeout=TIMEOUT)
    revealed: list = []
    s.reveal_requested.connect(revealed.append)
    current(view, 1, "tags")
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT):
        qtbot.keyClick(view.grid, Qt.Key.Key_Return)             # Enter on a container: reveal (as before)
    assert revealed[-1] == NodeRef(1, ("tags",))
    assert view.inline_editor() is None
    view.grid.activated.emit(3, col(view, "id"))                 # double click on a scalar: edit
    current(view, 3, "id")
    ed = inline(qtbot, view)
    assert ed.kind == "number" and ed.text() == "3"
    qtbot.keyClick(ed, Qt.Key.Key_Escape)                        # Esc: nothing changes
    qtbot.waitUntil(lambda: view.inline_editor() is None, timeout=TIMEOUT)
    assert not s.edit.dirty


def test_bool_toggles_and_null_opens_the_json_dialog(qtbot, opened, views, tmp_path) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "ok", "note"])
    current(view, 0, "ok")
    with applied(qtbot, s):
        view.edit_cell()                                         # bool: a toggle, no editor
    wait_text(qtbot, view, 0, "ok", "false")
    assert records(s)[0]["ok"] is False
    current(view, 0, "note")
    view.edit_cell()
    dlg = dialog(qtbot, view)
    assert isinstance(dlg, editors.JsonEditDialog) and dlg.text() == "null"
    with applied(qtbot, s):
        dlg.editor.setPlainText('{"a": [1.10, 1e400], "s": "Ўзбекистон"}')
        dlg.accept()
    rt = record_text(s, 0)
    assert "1.10" in rt and "1e400" in rt and "Ўзбекистон" in rt      # exact tokens, Cyrillic not escaped
    assert records(s)[0]["note"]["s"] == "Ўзбекистон"
    wait_text(qtbot, view, 0, "note", "{2}")


def test_shift_enter_edits_any_cell_as_json(qtbot, opened, views, tmp_path) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "contact", "price"])
    current(view, 0, "contact")
    qtbot.keyClick(view.grid, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    dlg = dialog(qtbot, view)
    assert json.loads(dlg.text()) == {"city": "Toshkent"}
    with applied(qtbot, s):
        dlg.editor.setPlainText('{"city": "Samarqand", "zip": 140100}')
        dlg.accept()
    assert records(s)[0]["contact"] == {"city": "Samarqand", "zip": 140100}
    wait_text(qtbot, view, 0, "contact", "{2}")
    current(view, 1, "price")                                    # a number as JSON: the exact token
    view.edit_as_json()
    dlg = dialog(qtbot, view)
    assert dlg.text() == "12345678901234567890"
    dlg.discard()


def test_string_with_line_breaks_uses_the_string_dialog(qtbot, opened, views, tmp_path) -> None:
    path = write_json(tmp_path / "s.json", [{"t": "line1\nline2"}, {"t": "x" * 300}])
    s = opened(path)
    view = table(qtbot, views, s, ["t"])
    current(view, 0, "t")
    view.edit_cell()
    dlg = dialog(qtbot, view)
    assert isinstance(dlg, editors.StringDialog) and dlg.value() == "line1\nline2"
    with applied(qtbot, s):
        dlg.set_text("bir\nikki\nuch")
        dlg.editor.setPlainText("bir\nikki\nuch 😀")
        dlg.accept()
    assert records(s)[0]["t"] == "bir\nikki\nuch 😀"
    current(view, 1, "t")                                        # longer than the inline limit
    view.edit_cell()
    dlg = dialog(qtbot, view)
    assert isinstance(dlg, editors.StringDialog) and dlg.value() == "x" * 300
    dlg.discard()


# ----------------------------------------------------------------------------------
# keys, index, missing fields, wildcards
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["keyed", "wrapped_obj"])
def test_key_column_renames_the_record_key(qtbot, opened, views, tmp_path, kind) -> None:
    recs = {"k0": {"v": 1}, "k1": {"v": 2}, "k2": {"v": 3}}
    if kind == "keyed":
        s = opened(write_json(tmp_path / "keyed.json", recs), format="keyed")
    else:
        s = opened(write_json(tmp_path / "w.json", {"meta": {"n": 1}, "data": recs}), format="wrapped",
                   record_path="data")
    view = table(qtbot, views, s, ["$key", "v", "$index"])
    current(view, 1, "$key")
    qtbot.keyClick(view.grid, Qt.Key.Key_F2)
    ed = inline(qtbot, view)
    assert ed.kind == "key" and ed.text() == "k1"
    commit_inline(qtbot, s, view, "yangi kalit — Тошкент")
    wait_text(qtbot, view, 1, "$key", "yangi kalit — Тошкент")
    assert s.doc.pinned().record_key(1) == "yangi kalit — Тошкент"
    # a key that exists already: refused in the editor (the error in place, no error window; review3 L1)
    current(view, 1, "$key")
    view.edit_cell()
    ed = inline(qtbot, view)
    ed.setText("k2")
    assert ed.commit() is False
    qtbot.waitUntil(lambda: not ed.checking, timeout=TIMEOUT)
    assert not ed.done and not ed.is_valid() and "k2" in ed.error_text()
    assert not s.edit.busy and not s.app.errors
    assert s.doc.pinned().record_key(1) == "yangi kalit — Тошкент"
    ed.cancel()
    # $index is read-only
    statuses = len(s.app.statuses)
    current(view, 1, "$index")
    assert view.edit_cell() is None
    assert len(s.app.statuses) > statuses
    assert view.edit_command_state("edit_value")[0] is False


def test_missing_field_is_created_with_set_path(qtbot, opened, views, tmp_path) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "note", "contact.city", "contact.geo.lat"])
    wait_loaded(qtbot, view, range(6), "note")
    assert view.cell_text(1, "note") == ""                       # record 1 has no note
    current(view, 1, "note")
    view.edit_cell()
    ed = inline(qtbot, view)                                     # the schema says: a string column
    assert ed.kind == "string" and ed.text() == ""
    commit_inline(qtbot, s, view, "yangi izoh")
    assert records(s)[1]["note"] == "yangi izoh"
    wait_text(qtbot, view, 1, "note", "yangi izoh")
    # missing intermediate objects are created
    current(view, 4, "contact.city")
    view.edit_cell()
    ed = inline(qtbot, view)
    commit_inline(qtbot, s, view, "Xiva")
    assert records(s)[4]["contact"] == {"city": "Xiva"}
    # an unknown column type: JSON text
    current(view, 0, "contact.geo.lat")
    view.edit_cell()
    dlg = dialog(qtbot, view)
    assert isinstance(dlg, editors.JsonEditDialog) and dlg.text() == ""
    with applied(qtbot, s):
        dlg.editor.setPlainText("41.311081")
        dlg.accept()
    assert record_text(s, 0).count("41.311081") == 1
    assert records(s)[0]["contact"]["geo"] == {"lat": records(s)[0]["contact"]["geo"]["lat"]}
    wait_text(qtbot, view, 0, "contact.geo.lat", "41.311081")


def test_wildcard_cells(qtbot, opened, views, tmp_path) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "tags[*]"])
    wait_loaded(qtbot, view, range(6), "tags[*]")
    current(view, 0, "tags[*]")                                  # exactly one match: its concrete path
    view.edit_cell()
    ed = inline(qtbot, view)
    assert ed.text() == "a"
    commit_inline(qtbot, s, view, "z")
    assert records(s)[0]["tags"] == ["z"]
    n = len(s.app.statuses)
    current(view, 1, "tags[*]")                                  # several: refused (Bulk operations)
    assert view.edit_cell() is None
    assert len(s.app.statuses) > n
    current(view, 2, "tags[*]")                                  # none: refused
    assert view.edit_cell() is None
    current(view, 1, "tags[*]")
    n = len(s.app.statuses)
    assert view.delete_cells() is None                           # Del on wildcards: refused with a message
    assert len(s.app.statuses) > n and s.edit.steps == 1           # (only the edit above)


# ----------------------------------------------------------------------------------
# Del, undo / redo
# ----------------------------------------------------------------------------------
def test_del_deletes_the_fields_of_the_selection_in_one_step(qtbot, opened, views, tmp_path) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "name", "note", "price"])
    wait_loaded(qtbot, view, range(6), "name")
    view.grid.setFocus()
    view.grid.set_current(0, col(view, "name"))
    view.grid.set_selected_ranges([(0, 2, col(view, "name"), col(view, "note"))])
    with applied(qtbot, s) as got:
        qtbot.keyClick(view.grid, Qt.Key.Key_Delete)
    assert len(got) == 1 and s.edit.steps == 1
    recs = records(s)
    for i in range(3):
        assert "name" not in recs[i]
        assert "note" not in recs[i]                             # (record 1 had no note: skipped)
    assert recs[3]["name"] == "Тошкент" and "price" in recs[0]
    wait_text(qtbot, view, 0, "name", "")
    wait_text(qtbot, view, 2, "note", "")
    # undo / redo refresh the rows
    with applied(qtbot, s):
        s.edit.undo()
    wait_text(qtbot, view, 0, "name", "Ali")
    wait_text(qtbot, view, 2, "note", "bor")
    with applied(qtbot, s):
        s.edit.redo()
    wait_text(qtbot, view, 1, "name", "")


def test_del_on_the_record_column_deletes_records(qtbot, opened, views, tmp_path) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["$", "id"])
    wait_loaded(qtbot, view, range(6), "id")
    view.grid.setFocus()
    view.grid.set_current(1, col(view, "$"))
    view.grid.set_selected_ranges([(1, 2, col(view, "$"), col(view, "$"))])
    with applied(qtbot, s):
        view.delete_selection()
    assert [r["id"] for r in records(s)] == [0, 3, 4, 5]
    qtbot.waitUntil(lambda: view.grid.row_count() == 4, timeout=TIMEOUT)
    wait_text(qtbot, view, 1, "id", "3")


# ----------------------------------------------------------------------------------
# records: delete / insert / duplicate / paste / move
# ----------------------------------------------------------------------------------
def test_record_commands(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "name"])
    wait_loaded(qtbot, view, range(6), "id")
    # delete two records (cell menu item)
    view.grid.setFocus()
    view.grid.set_current(1, col(view, "id"))
    view.grid.set_selected_ranges([(1, 2, 0, 1)])
    menu = view.cell_menu(1, col(view, "id"))
    act = next(a for a in menu.actions() if a.objectName() == "table_delete_records")
    assert act.isEnabled()
    with applied(qtbot, s):
        act.trigger()
    assert [r["id"] for r in records(s)] == [0, 3, 4, 5]
    qtbot.waitUntil(lambda: view.grid.row_count() == 4, timeout=TIMEOUT)
    wait_text(qtbot, view, 1, "id", "3")
    # duplicate (Ctrl+D) the current record
    view.grid.set_current(1, col(view, "name"))
    with applied(qtbot, s):
        qtbot.keyClick(view.grid, Qt.Key.Key_D, Qt.KeyboardModifier.ControlModifier)
    assert [r["id"] for r in records(s)] == [0, 3, 3, 4, 5]
    wait_text(qtbot, view, 2, "name", "Тошкент")
    # insert a record after the current row (Insert -> InsertDialog)
    view.grid.set_current(0, col(view, "id"))
    qtbot.keyClick(view.grid, Qt.Key.Key_Insert)
    dlg = dialog(qtbot, view)
    assert isinstance(dlg, editors.InsertDialog)
    dlg.set_value_type("json")
    dlg.json_edit.setPlainText('{"id": 99, "name": "Yangi"}')
    with applied(qtbot, s):
        dlg.accept()
    assert [r["id"] for r in records(s)] == [0, 99, 3, 3, 4, 5]
    wait_text(qtbot, view, 1, "name", "Yangi")
    # paste the clipboard's JSON as a record after the current row (Ctrl+V)
    QGuiApplication.clipboard().setText('{"id": 100, "name": "Bufer"}')
    view.grid.set_current(5, col(view, "id"))
    with applied(qtbot, s):
        qtbot.keyClick(view.grid, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert [r["id"] for r in records(s)][-1] == 100
    # move the current record up (base view)
    view.grid.set_current(1, col(view, "id"))
    assert view.edit_command_state("move_up") == (True, "")
    with applied(qtbot, s):
        view.move_selection(-1)
    assert [r["id"] for r in records(s)][:2] == [99, 0]
    # undo everything: the table shows the original records again
    while s.edit.can_undo:
        with applied(qtbot, s):
            s.edit.undo()
    assert [r["id"] for r in records(s)] == [0, 1, 2, 3, 4, 5]
    qtbot.waitUntil(lambda: view.grid.row_count() == 6, timeout=TIMEOUT)
    wait_text(qtbot, view, 1, "name", "Vali")


def test_current_record_follows_inserts_and_deletes(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    path = write_json(tmp_path / "n.json", [{"id": i, "name": f"n{i}"} for i in range(200)], indent=None)
    s = opened(path)
    view = table(qtbot, views, s, ["id", "name"])
    view.grid.scroll_to(50, "top")
    wait_loaded(qtbot, view, range(50, 60), "id")
    current(view, 55, "name")
    qtbot.waitUntil(lambda: s.current == NodeRef(55, ("name",)), timeout=TIMEOUT)
    # the session selects the inserted record: the table follows it like any selection
    with applied(qtbot, s):
        s.edit.insert(None, 0, '{"id": -1, "name": "new"}')
    qtbot.waitUntil(lambda: view.grid.current()[0] == 0, timeout=TIMEOUT)
    wait_text(qtbot, view, 0, "name", "new")
    assert s.current.record == 0
    # without a selection from the session (e.g. it equals the current node): the current record and the
    # first shown record are remapped
    monkeypatch.setattr(s, "select", lambda *a, **k: None)
    view.grid.scroll_to(50, "top")
    wait_loaded(qtbot, view, range(50, 60), "id")
    current(view, 55, "name")
    top = view.grid.top_row()
    top_id = view.cell_text(top, "id")
    with applied(qtbot, s):                                      # 3 records inserted above the screen
        s.edit.paste(None, '[{"id": -2}, {"id": -3}, {"id": -4}]', index=1, as_records=True)
    qtbot.waitUntil(lambda: view.record_at_row(view.grid.current()[0]) == 58, timeout=TIMEOUT)
    wait_text(qtbot, view, view.grid.current()[0], "name", "n54")
    qtbot.waitUntil(lambda: view.cell_text(view.grid.top_row(), "id") == top_id, timeout=TIMEOUT)
    with applied(qtbot, s):                                      # the current record deleted elsewhere
        s.edit.delete([NodeRef(58, ())])
    qtbot.waitUntil(lambda: view.record_at_row(view.grid.current()[0]) == 58, timeout=TIMEOUT)
    wait_text(qtbot, view, view.grid.current()[0], "name", "n55")      # its neighbour
    assert records(s)[58]["id"] == 55


def test_records_in_filtered_and_sorted_views(qtbot, opened, views, tmp_path) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "name", "ok"])
    view.apply_quick_filter("ok", "=true")
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 4, timeout=TIMEOUT)
    wait_loaded(qtbot, view, range(4), "id")
    assert [view.cell_text(r, "id") for r in range(4)] == ["0", "2", "4", "5"]
    # delete the second row of the filtered view (record 2)
    view.grid.setFocus()
    view.grid.set_current(1, col(view, "id"))
    with applied(qtbot, s):
        view.delete_records()
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 3, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: [view.cell_text(r, "id") for r in range(3)] == ["0", "4", "5"], timeout=TIMEOUT)
    assert [view.record_at_row(r) for r in range(3)] == [0, 3, 4]     # positions after the delete
    # an edit that makes a record leave the filter: the view keeps it until refreshed, the cell shows the value
    view.grid.set_current(0, col(view, "ok"))
    with applied(qtbot, s):
        view.edit_cell()
    wait_text(qtbot, view, 0, "ok", "false")
    # a sorted view: duplicate a record; positions stay consistent with the view set
    view.clear_quick_filters()
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 5, timeout=TIMEOUT)
    s.set_sort([SortKey("name", descending=True)])
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    wait_loaded(qtbot, view, range(5), "name")
    names = [view.cell_text(r, "name") for r in range(5)]
    assert names == sorted(names, reverse=True)
    view.grid.set_current(0, col(view, "name"))
    with applied(qtbot, s):
        view.duplicate_records()
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    rs = s.view_recordset()
    qtbot.waitUntil(lambda: all(view.record_at_row(r) == int(rs[r]) for r in range(s.view_len())),
                    timeout=TIMEOUT)
    for r in range(s.view_len()):
        rec = int(rs[r])
        wait_text(qtbot, view, r, "id", str(records(s)[rec]["id"]))


def test_big_record_selections_use_the_record_tasks(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tableview, "EDIT_SELECTION_MAX", 3)
    monkeypatch.setattr(tableview, "DELETE_CONFIRM_RECORDS", 3)
    asked: list = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: asked.append(text) or True)
    path = write_json(tmp_path / "many.json", [{"i": i, "even": i % 2 == 0} for i in range(40)], indent=None)
    s = opened(path)
    view = table(qtbot, views, s, ["i", "even"])
    # base view: the rows are the positions
    view.grid.setFocus()
    view.grid.set_current(0, 0)
    view.grid.set_selected_ranges([(0, 9, 0, 1)])
    with applied(qtbot, s):
        view.duplicate_records()
    assert len(records(s)) == 50
    assert [r["i"] for r in records(s)][:4] == [0, 0, 1, 1]
    # a filtered view: the records are collected in a task (RecordSet of the applied gen)
    view.apply_quick_filter("even", "=true")                       # 20 + 5 duplicated even records
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 25, timeout=TIMEOUT)
    view.grid.set_current(0, 0)
    view.grid.set_selected_ranges([(0, 7, 0, 1)])
    with applied(qtbot, s):
        view.delete_records()
    assert asked                                                 # more than DELETE_CONFIRM_RECORDS: asked
    remaining = records(s)
    assert len(remaining) == 42
    assert sum(1 for r in remaining if r["even"]) == 17
    assert [r["i"] for r in remaining if r["even"]][:3] == [8, 8, 10]
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 17, timeout=TIMEOUT)


# ----------------------------------------------------------------------------------
# document kinds
# ----------------------------------------------------------------------------------
KINDS = {
    "jsonl": ("d.jsonl", '{"id": 0, "s": "a"}\n{"id": 1, "s": "b"}\n{"id": 2, "s": "c"}\n', {}),
    "wrapped": ("w.json", '{"meta": {"v": 1}, "data": [{"id": 0, "s": "a"}, {"id": 1, "s": "b"}, '
                '{"id": 2, "s": "c"}], "tail": true}', {"format": "wrapped", "record_path": "data"}),
    "brace_list": ("b.json", '{{"id": 0, "s": "a"}, {"id": 1, "s": "b"}, {"id": 2, "s": "c"}}', {}),
    "stream": ("st.json", '{"id": 0, "s": "a"}\n\n{"id": 1, "s": "b"}\n{"id": 2, "s": "c"}', {"format": "stream"}),
}


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_cell_edit_in_every_record_kind(qtbot, opened, views, tmp_path, kind) -> None:
    name, text, kw = KINDS[kind]
    s = opened(write(tmp_path / name, text), **kw)
    view = table(qtbot, views, s, ["id", "s"])
    current(view, 1, "s")
    view.edit_cell()
    commit_inline(qtbot, s, view, "Ўзбек")
    wait_text(qtbot, view, 1, "s", "Ўзбек")
    assert [r["s"] for r in records(s)] == ["a", "Ўзбек", "c"]
    view.grid.set_current(2, col(view, "id"))
    with applied(qtbot, s):
        view.delete_records()
    qtbot.waitUntil(lambda: view.grid.row_count() == 2, timeout=TIMEOUT)
    assert [r["id"] for r in records(s)] == [0, 1]


def test_single_document(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "one.json", {"a": 1, "b": {"c": "x"}}), format="single")
    view = table(qtbot, views, s, ["a", "b.c", "b.d"])
    current(view, 0, "b.c")
    view.edit_cell()
    commit_inline(qtbot, s, view, "y")
    assert records(s)[0] == {"a": 1, "b": {"c": "y"}}
    wait_text(qtbot, view, 0, "b.c", "y")                        # the edited row is loaded again (else: "loading")
    current(view, 0, "b.d")                                      # missing: set_path in the single value
    view.edit_cell()
    dlg = dialog(qtbot, view)
    with applied(qtbot, s):
        dlg.editor.setPlainText("[1, 2]")
        dlg.accept()
    assert records(s)[0]["b"]["d"] == [1, 2]
    n = len(s.app.statuses)
    assert view.duplicate_records() is None and view.insert_record() is None
    assert len(s.app.statuses) >= n + 2
    assert view.edit_command_state("duplicate")[0] is False


# ----------------------------------------------------------------------------------
# editors and data changes
# ----------------------------------------------------------------------------------
def test_inline_editor_is_cancelled_when_its_record_changes(qtbot, opened, views, tmp_path) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "name"])
    current(view, 1, "name")
    view.edit_cell()
    ed = inline(qtbot, view)
    ed.setText("half typed")
    with applied(qtbot, s):                                      # another record changed: the editor stays
        s.edit.set_value(NodeRef(4, ("name",)), '"Olim2"')
    assert view.inline_editor() is ed
    with applied(qtbot, s):                                      # its own record changed: cancelled
        s.edit.set_value(NodeRef(1, ("id",)), "11")
    qtbot.waitUntil(lambda: view.inline_editor() is None, timeout=TIMEOUT)
    assert records(s)[1]["name"] == "Vali"


def test_dialog_keeps_its_node_across_structural_changes(qtbot, opened, views, tmp_path) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "contact"])
    current(view, 3, "contact")
    view.edit_value()                                            # a container: JSON text
    dlg = dialog(qtbot, view)
    with applied(qtbot, s):                                      # a record inserted above it
        s.edit.insert(None, 0, '{"id": -1}')
    with applied(qtbot, s):
        dlg.editor.setPlainText('{"city": "Nukus"}')
        dlg.accept()
    recs = records(s)
    assert recs[4]["id"] == 3 and recs[4]["contact"] == {"city": "Nukus"}
    # a dialog whose node was deleted meanwhile changes nothing
    current(view, 1, "contact")
    view.edit_value()
    dlg = dialog(qtbot, view)
    with applied(qtbot, s):
        s.edit.delete([NodeRef(1, ())])
    steps = s.edit.steps
    n = len(s.app.statuses)
    dlg.editor.setPlainText('{"x": 1}')
    dlg.accept()
    qtbot.wait(100)
    assert s.edit.steps == steps and len(s.app.statuses) > n


def test_node_menu_has_the_edit_group(qtbot, opened, views, tmp_path) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "name", "tags[*]"])
    wait_loaded(qtbot, view, range(3), "tags[*]")
    current(view, 0, "name")
    menu = view.cell_menu(0, col(view, "name"))
    names = {a.objectName(): a for a in menu.actions()}
    assert names["edit_edit_value"].isEnabled()
    assert names["edit_rename"].isEnabled()
    assert names["edit_delete"].isEnabled()
    assert names["table_delete_records"].isEnabled()
    current(view, 1, "tags[*]")
    menu = view.cell_menu(1, col(view, "tags[*]"))
    names = {a.objectName(): a for a in menu.actions()}
    assert not names["edit_edit_value"].isEnabled() and names["edit_edit_value"].toolTip()


def test_no_large_reads_in_the_gui_thread(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    big = [{"id": i, "s": "x" * 50_000, "n": i} for i in range(6)]          # ~300 KB
    path = write_json(tmp_path / "big.json", big, indent=None)
    probe = GuiReads(monkeypatch)
    s = opened(path)
    view = table(qtbot, views, s, ["id", "s", "n"])
    current(view, 1, "n")
    view.edit_cell()
    commit_inline(qtbot, s, view, "77")
    current(view, 2, "s")
    view.edit_cell()                                             # 50 KB string: the string dialog
    dlg = dialog(qtbot, view)
    dlg.discard()
    view.grid.set_current(0, col(view, "n"))
    view.grid.set_selected_ranges([(0, 3, col(view, "s"), col(view, "n"))])
    with applied(qtbot, s):
        view.delete_cells()
    with applied(qtbot, s):
        s.edit.undo()
    view.grid.set_current(4, 0)
    with applied(qtbot, s):
        view.duplicate_records()
    assert probe.big() == []


def test_del_with_whole_records_and_their_fields(qtbot, opened, views, tmp_path) -> None:
    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["$", "id", "name"])
    wait_loaded(qtbot, view, range(6), "id")
    view.grid.setFocus()
    view.grid.set_current(0, 0)
    view.grid.set_selected_ranges([(0, 1, 0, 2)])                 # "$" + fields of the same records
    with applied(qtbot, s):
        qtbot.keyClick(view.grid, Qt.Key.Key_Delete)
    assert [r["id"] for r in records(s)] == [2, 3, 4, 5]          # the records go (their fields with them)
    assert s.edit.steps == 1


def test_bulk_selection_scope(qtbot, opened, views, tmp_path) -> None:
    from jsonreshape.gui.bulkdialog import SelectionScope

    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "ok"])
    view.grid.setFocus()
    view.grid.set_current(1, 0)
    view.grid.set_selected_ranges([(1, 2, 0, 0), (4, 4, 1, 1)])
    scope = view.bulk_selection()
    assert isinstance(scope, SelectionScope) and len(scope) == 3
    assert [list(c) for c in scope.iter_chunks()] == [[1, 2], [4]]
    view.apply_quick_filter("ok", "=true")
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 4, timeout=TIMEOUT)
    view.grid.set_current(1, 0)
    view.grid.set_selected_ranges([(1, 3, 0, 0)])
    scope = view.bulk_selection()
    assert sorted(int(x) for c in scope.iter_chunks() for x in c) == [2, 4, 5]


def test_new_record_outside_a_filtered_view_is_reported(qtbot, opened, views, tmp_path) -> None:
    from jsonreshape.i18n import t

    s = opened(people_file(tmp_path))
    view = table(qtbot, views, s, ["id", "ok"])
    view.apply_quick_filter("ok", "=false")
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 2, timeout=TIMEOUT)
    view.grid.setFocus()
    view.grid.set_current(0, 0)
    with applied(qtbot, s):
        view.duplicate_records()
    assert [r["id"] for r in records(s)] == [0, 1, 1, 2, 3, 4, 5]
    qtbot.waitUntil(lambda: t("gui.table.records.not_in_view") in s.app.statuses, timeout=TIMEOUT)
    assert s.view_len() == 2                                       # until the view is computed again


@pytest.mark.parametrize("backend", ["rust", "python"])
def test_streamed_records_after_edits(qtbot, opened, views, tmp_path, monkeypatch, backend) -> None:
    from jsonreshape import core

    if backend == "rust" and "rust" not in core.available_backends():
        pytest.skip("the Rust core is not available")
    monkeypatch.setattr(core, "BACKEND", backend)               # the table's fast path or the stream path
    big = [{"id": i, "name": f"n{i}", "pad": ["x" * 1000] * 1500, "w": [{"x": i}]} for i in range(3)]
    s = opened(write_json(tmp_path / "big.json", big, indent=None))   # ~1.5 MB records: streamed cells
    view = table(qtbot, views, s, ["id", "name", "pad", "w[*].x"])
    wait_text(qtbot, view, 1, "pad", "[1500]")
    current(view, 1, "name")
    view.edit_cell()
    commit_inline(qtbot, s, view, "Ўзбек 😀")
    wait_text(qtbot, view, 1, "name", "Ўзбек 😀")                   # the edited unit, streamed again
    wait_text(qtbot, view, 1, "pad", "[1500]")
    assert s.doc.pinned().record_span(1)[0] >= core_edit_base()
    current(view, 2, "w[*].x")                                      # one wildcard match in a streamed row
    view.edit_cell()
    commit_inline(qtbot, s, view, "1e400")
    assert '"w": [{"x": 1e400}]' in record_text(s, 2)              # the exact token in the data
    # (the stream path of the pure-Python core shows ijson's normalized exponent: stream.py)
    wait_text(qtbot, view, 2, "w[*].x", "1e400" if backend == "rust" else "1E+400")


def core_edit_base() -> int:
    from jsonreshape.overlay import EDIT_BASE

    return EDIT_BASE
