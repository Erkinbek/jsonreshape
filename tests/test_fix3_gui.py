"""Stage 3 fix round, GUI views (review3_gui H1, M1, M2, L1, L2, L3, L5, L7).

* H1: a reopen that indexes the file again (every save reopens the saved file) never leaves rows showing a cached
  read error: the tree forgets its old window instead of fetching it from the new document, records the index has
  not reached yet are "not loaded" (never an error), errors cached while indexing are read again when it ends.
* M1: keys typed into a line edit that lives inside a grid (inline editors, the table's quick filter row) never run
  the grid's edit commands.
* M2: after Del on the current node the text view shows the node that took its place (the tree leaves the selection
  of ``cs.select`` to the session; the text view takes refs of the new state as such).
* L1: duplicate keys are refused in the editors (inline error, no modal "Xato"), at the member and the record level.
* L2: a tree dialog accepted after an edit was applied acts on its (remapped) node, or says it is gone.
* L3: "change type" offers no conversion §12.4 always refuses.
* L5: F2 renames in the table; the table's menus show no Alt+Up/Down keys.
* L7: protected rows say why Del / Ctrl+D do nothing; Enter toggles them.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

import shiboken6  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu  # noqa: E402

from jsonreshape.gui import editors, widgets  # noqa: E402
from jsonreshape.gui.session import NodeRef, same_node  # noqa: E402
from jsonreshape.gui.tableview import TableView  # noqa: E402
from jsonreshape.gui.textview import TextView  # noqa: E402
from jsonreshape.gui.treemodel import NodeInfo  # noqa: E402
from jsonreshape.gui.treeview import TreeView  # noqa: E402
from jsonreshape.i18n import t  # noqa: E402
from jsonreshape.reader import Document  # noqa: E402

from views_edit_helpers import (  # noqa: E402,F401
    TIMEOUT,
    app,
    applied,
    no_logged_errors,
    opened,
    records,
    views,
    write_json,
)

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("no_logged_errors")]

K = Qt.Key
M = Qt.KeyboardModifier


@pytest.fixture(autouse=True)
def _no_modal_confirm(monkeypatch):
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: True)


def people(n: int) -> list[dict]:
    return [{"id": i, "name": f"n{i}", "city": "Xiva" if i % 2 else "Toshkent", "tags": ["a"] if i % 2 else [],
             "contact": {"phone": f"+998{i:07d}", "zip": 100000 + i}} for i in range(n)]


@pytest.fixture
def gated_index(monkeypatch):
    """``arm() -> Event``: the next ``Document.build_index`` waits until the event is set (a document that is
    still being indexed while the views already show it, deterministically)."""
    real = Document.build_index
    armed: list[threading.Event] = []
    made: list[threading.Event] = []

    def build_index(self, *args, **kw):
        gate = armed.pop(0) if armed else None
        if gate is not None:
            gate.wait(30)
        return real(self, *args, **kw)

    monkeypatch.setattr(Document, "build_index", build_index)

    def arm() -> threading.Event:
        gate = threading.Event()
        armed.append(gate)
        made.append(gate)
        return gate

    yield arm
    for gate in made:
        gate.set()


def settle(qtbot, app) -> None:
    """Every task ended and its callbacks ran (no sleeping: events are processed until idle)."""
    assert app.tasks.wait_idle(TIMEOUT / 1000)


# ----------------------------------------------------------------------------------
# tree helpers
# ----------------------------------------------------------------------------------
def tree_rows_loaded(qtbot, view: TreeView, n: int) -> list:
    """The value cells of the first ``n`` rows once all of them are loaded."""
    qtbot.waitUntil(lambda: view.row_count() >= n and all(view.cell_at(r, "value") is not None for r in range(n)),
                    timeout=TIMEOUT)
    return [view.cell_at(r, "value") for r in range(n)]


def tree_select(qtbot, view: TreeView, kp: tuple) -> int:
    qtbot.waitUntil(lambda: view.row_of(kp) is not None, timeout=TIMEOUT)
    row = view.row_of(kp)
    qtbot.waitUntil(lambda: view.cell_text(row, "key") is not None, timeout=TIMEOUT)
    view.select_row(row, scroll="ensure")
    qtbot.waitUntil(lambda: view.current_keypath() == kp, timeout=TIMEOUT)
    return row


def tree_expand(qtbot, view: TreeView, kp: tuple) -> None:
    qtbot.waitUntil(lambda: view.row_of(kp) is not None, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: view.cell_text(view.row_of(kp), "key") is not None, timeout=TIMEOUT)
    view.expand_node(kp)
    qtbot.waitUntil(lambda: view.flat.is_expanded(kp) and not view.is_loading(kp), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not view.busy(), timeout=TIMEOUT)


def press(widget, key, mods=M.NoModifier) -> None:
    widget.setFocus()
    QTest.keyClick(widget, key, mods)


def focused(qtbot, widget) -> None:
    widget.activateWindow()
    widget.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(widget.hasFocus, timeout=TIMEOUT)


def visible_menus(view) -> list[QMenu]:
    return [m for m in view.findChildren(QMenu) if m.isVisible()]


# ----------------------------------------------------------------------------------
# H1: rows after a reopen that indexes the file again
# ----------------------------------------------------------------------------------
def test_tree_rows_after_a_reopen_that_indexes_again(qtbot, app, opened, views, gated_index, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(12)))
    view = views(TreeView, s)
    tree_rows_loaded(qtbot, view, 12)
    gate = gated_index()
    with qtbot.waitSignal(s.opened, timeout=TIMEOUT):
        s.reopen(use_cache=False)
    assert not s.doc.index_complete                       # the new document is being indexed (0 records yet)
    qtbot.waitUntil(lambda: view._loader.pending() == 0, timeout=TIMEOUT)   # whatever the tree asked for meanwhile
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        gate.set()
    cells = tree_rows_loaded(qtbot, view, 12)
    assert [c.role for c in cells if c.role == "error"] == [], [c.text for c in cells]
    assert view.cell_text(3, "value").startswith("{5} id: 3")


def test_tree_load_errors_from_the_indexing_time_are_read_again(qtbot, app, opened, views, gated_index,
                                                                 tmp_path) -> None:
    """Whatever failed while the index was growing (here: forced) is fetched again when the index is final."""
    s = opened(write_json(tmp_path / "p.json", people(6)))
    view = views(TreeView, s)
    tree_rows_loaded(qtbot, view, 6)
    gate = gated_index()
    with qtbot.waitSignal(s.opened, timeout=TIMEOUT):
        s.reopen(use_cache=False)
    from jsonreshape.gui.loader import LoadFailed

    loader = view._loader
    loader.request([(0,), (1,)])
    qtbot.waitUntil(lambda: loader.pending() == 0, timeout=TIMEOUT)
    with loader._state.lock:                              # a failure cached while indexing
        loader._state.store((0,), LoadFailed(IndexError("record 0 out of range (0 records)")), 0)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        gate.set()
    cells = tree_rows_loaded(qtbot, view, 6)
    qtbot.waitUntil(lambda: isinstance(loader.peek((0,)), NodeInfo), timeout=TIMEOUT)
    assert view.cell_at(0, "value").role != "error", [c.text for c in cells]


def test_table_rows_after_a_reopen_that_indexes_again(qtbot, app, opened, views, gated_index, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(12)))
    view = views(TableView, s)
    view.set_columns(["id", "name"])
    qtbot.waitUntil(lambda: all(view.cell_text(r, "name") == f"n{r}" for r in range(12)), timeout=TIMEOUT)
    gate = gated_index()
    with qtbot.waitSignal(s.opened, timeout=TIMEOUT):
        s.reopen(use_cache=False)
    assert not s.doc.index_complete
    qtbot.waitUntil(lambda: view._loader is not None and view._loader.pending() == 0, timeout=TIMEOUT)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        gate.set()
    qtbot.waitUntil(lambda: view.row_count() == 12 and all(view.cell_text(r, "name") is not None for r in range(12)),
                    timeout=TIMEOUT)
    assert [view.cell_text(r, "name") for r in range(12)] == [f"n{r}" for r in range(12)]
    assert all(view.cell_at(r, "id").role != "error" for r in range(12))


# ----------------------------------------------------------------------------------
# M1: keys typed into line edits inside a grid stay there
# ----------------------------------------------------------------------------------
EDIT_KEYS = ((K.Key_F2, M.NoModifier), (K.Key_Insert, M.NoModifier), (K.Key_D, M.ControlModifier),
             (K.Key_Down, M.AltModifier), (K.Key_Up, M.AltModifier), (K.Key_V, M.ControlModifier))


def no_dialogs(view) -> bool:
    from PySide6.QtWidgets import QDialog

    return not [d for d in view.findChildren(QDialog) if d.isVisible()]


def test_tree_inline_editor_keeps_the_edit_keys(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(4)))
    view = views(TreeView, s)
    tree_expand(qtbot, view, (1,))
    tree_select(qtbot, view, (1, 1))                      # "name"
    press(view.grid, K.Key_Return)
    qtbot.waitUntil(lambda: view.inline_editor() is not None, timeout=TIMEOUT)
    ed = view.inline_editor()
    assert ed.kind == "string"
    focused(qtbot, ed)
    QTest.keyClicks(ed, "abc")
    QApplication.clipboard().setText("")
    for key, mods in EDIT_KEYS:
        QTest.keyClick(ed, key, mods)
        settle(qtbot, app)
        assert view.inline_editor() is ed and not ed.done and ed.kind == "string", (key, mods)
        assert not visible_menus(view) and no_dialogs(view), (key, mods)
        assert not s.edit.busy and s.edit.steps == 0, (key, mods)
    assert ed.text().endswith("abc")
    QTest.keyClick(ed, K.Key_Escape)
    qtbot.waitUntil(lambda: view.inline_editor() is None, timeout=TIMEOUT)
    # the same keys typed into the grid itself still run the commands
    press(view.grid, K.Key_Insert)
    qtbot.waitUntil(lambda: bool(visible_menus(view)), timeout=TIMEOUT)
    for menu in visible_menus(view):
        menu.close()


def test_tree_inline_editor_keeps_keys_sent_without_focus(qtbot, app, opened, views, tmp_path) -> None:
    """A key event delivered straight to the editor (no focus, e.g. another window is active) stays there too."""
    s = opened(write_json(tmp_path / "p.json", people(3)))
    view = views(TreeView, s)
    tree_expand(qtbot, view, (0,))
    tree_select(qtbot, view, (0, 1))
    press(view.grid, K.Key_Return)
    qtbot.waitUntil(lambda: view.inline_editor() is not None, timeout=TIMEOUT)
    ed = view.inline_editor()
    view.grid.setFocus()                                   # the focus is NOT in the editor
    for key, mods in ((K.Key_F2, M.NoModifier), (K.Key_D, M.ControlModifier), (K.Key_Insert, M.NoModifier)):
        QTest.keyClick(ed, key, mods)
        settle(qtbot, app)
        assert not s.edit.busy and s.edit.steps == 0 and not visible_menus(view), (key, mods)
        assert view.inline_editor() in (ed, None) and (view.inline_editor() is None or view.inline_editor().kind
                                                        == "string")
    ed.cancel()


def test_table_inline_editor_keeps_the_edit_keys(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(5)))
    view = views(TableView, s)
    view.set_columns(["id", "name", "city"])
    qtbot.waitUntil(lambda: view.cell_text(2, "name") == "n2", timeout=TIMEOUT)
    view.grid.setFocus()
    view.grid.set_current(2, next(i for i, c in enumerate(view.grid.columns()) if c.id == "name"))
    press(view.grid, K.Key_Return)
    qtbot.waitUntil(lambda: view.inline_editor() is not None, timeout=TIMEOUT)
    ed = view.inline_editor()
    focused(qtbot, ed)
    QTest.keyClicks(ed, "xyz")
    QApplication.clipboard().setText("")
    for key, mods in EDIT_KEYS + ((K.Key_Return, M.ShiftModifier),):
        QTest.keyClick(ed, key, mods)
        settle(qtbot, app)
        if key == K.Key_Return:
            break                                          # Shift+Enter commits like Enter (the editor's own key)
        assert view.inline_editor() is ed and not ed.done, (key, mods)
        assert view.edit_dialog() is None and no_dialogs(view), (key, mods)
        assert not s.edit.busy and s.edit.steps == 0, (key, mods)


def test_table_quick_filter_keeps_the_edit_keys(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(5)))
    view = views(TableView, s)
    view.set_columns(["id", "name", "city"])
    qtbot.waitUntil(lambda: view.cell_text(2, "name") == "n2", timeout=TIMEOUT)
    name_col = next(i for i, c in enumerate(view.grid.columns()) if c.id == "name")
    view.grid.set_current(2, name_col)
    press(view.grid, K.Key_Down, M.AltModifier)            # Alt+Down in the grid: the column's quick filter
    qf = view.filter_editor("name")
    qtbot.waitUntil(qf.hasFocus, timeout=TIMEOUT)
    QApplication.clipboard().setText("")
    for key, mods in EDIT_KEYS + ((K.Key_Return, M.ShiftModifier), (K.Key_Delete, M.NoModifier)):
        focused(qtbot, qf)
        QTest.keyClick(qf, key, mods)
        settle(qtbot, app)
        assert view.inline_editor() is None and view.edit_dialog() is None and no_dialogs(view), (key, mods)
        assert not s.edit.busy and s.edit.steps == 0, (key, mods)
        assert view.grid.current() == (2, name_col), (key, mods)


# ----------------------------------------------------------------------------------
# M2: the text view after Del on the current node
# ----------------------------------------------------------------------------------
def text_settled(qtbot, view: TextView) -> str:
    qtbot.waitUntil(lambda: not view.is_loading() and (view.result() is not None or view.node_deleted()),
                    timeout=TIMEOUT)
    return view.text()


@pytest.mark.parametrize("target", ["record", "member"])
def test_text_view_follows_the_node_that_took_the_deleted_ones_place(qtbot, app, opened, views, tmp_path,
                                                                    target) -> None:
    s = opened(write_json(tmp_path / "p.json", people(6)))
    tree = views(TreeView, s)
    text = views(TextView, s)
    if target == "record":
        kp, before, after = (3,), '"n3"', '"n4"'
    else:
        tree_expand(qtbot, tree, (1,))
        kp, before, after = (1, 3), '[\n  "a"\n]', '"phone": "+9980000001"'    # tags; contact takes its place
    tree_select(qtbot, tree, kp)
    qtbot.waitUntil(lambda: before in text_settled(qtbot, text), timeout=TIMEOUT)
    with applied(qtbot, s):
        press(tree.grid, K.Key_Delete)
    shown = text_settled(qtbot, text)
    assert not text.node_deleted(), shown
    assert same_node(text.shown_ref(), s.current), (text.shown_ref(), s.current)
    assert after in shown and before not in shown, shown
    if target == "member":
        assert s.current.path == ("contact",)
    assert tree.current_keypath() == kp                    # the tree stays on the row that took the place


def test_text_view_takes_a_selection_made_inside_data_changed_as_new_state(qtbot, app, opened, views,
                                                                            tmp_path) -> None:
    """A view whose ``data_changed`` slot runs before the text view's selects the ChangeSet's node: the text
    view shows that node (it neither remaps it as an old ref nor reports it deleted)."""
    from jsonreshape.gui.editing import from_edit_ref

    s = opened(write_json(tmp_path / "p.json", people(6)))

    def early(cs) -> None:                                 # connected before the text view: runs first
        if getattr(cs, "select", None) is not None:
            s.select(from_edit_ref(s, cs.select), origin=early, history=False)

    s.data_changed.connect(early)
    try:
        text = views(TextView, s)
        s.select(NodeRef(3, (), ()))
        qtbot.waitUntil(lambda: '"n3"' in text_settled(qtbot, text), timeout=TIMEOUT)
        with applied(qtbot, s):
            s.edit.delete([NodeRef(3, (), ())])
        shown = text_settled(qtbot, text)
        assert not text.node_deleted() and '"n4"' in shown
        assert same_node(text.shown_ref(), NodeRef(3, (), ()))
        # an unrelated edit while a node of another record is current: remapped as before
        s.select(NodeRef(4, ("name",), (1,)))
        qtbot.waitUntil(lambda: text_settled(qtbot, text) == '"n5"', timeout=TIMEOUT)
        with applied(qtbot, s):
            s.edit.delete([NodeRef(0, (), ())])          # selects record 0's successor, not the shown node
        qtbot.waitUntil(lambda: not text.is_loading(), timeout=TIMEOUT)
        assert same_node(text.shown_ref(), s.current)
        assert not text.node_deleted()
    finally:
        s.data_changed.disconnect(early)


# ----------------------------------------------------------------------------------
# L1: duplicate keys are refused in the editors (inline error, no error dialog)
# ----------------------------------------------------------------------------------
def visible_dialog(qtbot, view, cls):
    def find():
        return [d for d in view.findChildren(cls) if d.isVisible()]

    qtbot.waitUntil(lambda: bool(find()), timeout=TIMEOUT)
    return find()[-1]


def keyed_file(tmp_path: Path, n: int = 4) -> Path:
    return write_json(tmp_path / "keyed.json", {f"k{i}": {"v": i, "o": {"a": 1, "b": 2}} for i in range(n)})


def test_tree_inline_key_editor_refuses_a_loaded_sibling_key(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(3)))
    view = views(TreeView, s)
    tree_expand(qtbot, view, (0,))
    tree_select(qtbot, view, (0, 2))                      # "city"
    press(view.grid, K.Key_F2)
    qtbot.waitUntil(lambda: view.inline_editor() is not None, timeout=TIMEOUT)
    ed = view.inline_editor()
    assert ed.kind == "key" and ed.text() == "city"
    ed.setText("name")                                     # a sibling shown in the tree: refused at once
    assert not ed.is_valid() and "name" in ed.error_text()
    assert ed.commit() is False and not ed.done
    settle(qtbot, app)
    assert view.inline_editor() is ed and s.edit.steps == 0 and not app.errors
    with applied(qtbot, s):
        ed.setText("shahar")
        ed.commit()
    assert list(records(s)[0]) == ["id", "name", "shahar", "tags", "contact"]


def test_tree_key_check_refuses_keys_of_members_not_loaded(qtbot, app, opened, views, tmp_path) -> None:
    """Insert into a collapsed object: its members are not loaded, the key is looked up when OK is pressed; a
    taken key shows the error in the dialog (no "Xato" window), a free one is inserted."""
    s = opened(write_json(tmp_path / "p.json", people(3)))
    view = views(TreeView, s)
    tree_expand(qtbot, view, (0,))
    tree_select(qtbot, view, (0, 4))                      # "contact" (collapsed): phone, zip
    dlg = view.insert_node("child")
    assert isinstance(dlg, editors.InsertDialog) and dlg.key_needed()
    dlg.key_edit.setText("phone")
    dlg.string_edit.setText("x")
    assert dlg.ok_button.isEnabled()                      # not known yet: asked on OK
    dlg.accept()
    qtbot.waitUntil(lambda: not dlg.checking, timeout=TIMEOUT)
    settle(qtbot, app)
    assert dlg.isVisible() and not dlg.ok_button.isEnabled()
    assert t("gui.editors.error.key_exists", key="phone") in dlg.error_label.text()
    assert s.edit.steps == 0 and not app.errors
    with applied(qtbot, s):
        dlg.key_edit.setText("email")
        dlg.accept()
    assert records(s)[0]["contact"] == {"phone": "+9980000000", "zip": 100000, "email": "x"}
    assert not shiboken6.isValid(dlg) or not dlg.isVisible()      # closed (and deleted)


def test_tree_record_key_rename_is_checked_before_the_commit(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(keyed_file(tmp_path), format="keyed")
    view = views(TreeView, s)
    tree_select(qtbot, view, (1,))                        # record "k1"
    press(view.grid, K.Key_F2)
    qtbot.waitUntil(lambda: view.inline_editor() is not None, timeout=TIMEOUT)
    ed = view.inline_editor()
    assert ed.kind == "key" and ed.text() == "k1"
    ed.setText("k3")                                       # another record's key
    assert ed.commit() is False
    qtbot.waitUntil(lambda: not ed.checking, timeout=TIMEOUT)
    settle(qtbot, app)
    assert view.inline_editor() is ed and not ed.done and not ed.is_valid()
    assert "k3" in ed.error_text() and s.edit.steps == 0 and not app.errors
    with applied(qtbot, s):
        ed.setText("yangi")
        ed.commit()                                        # free: committed after the check
    assert [s.doc.pinned().record_key(i) for i in range(4)] == ["k0", "yangi", "k2", "k3"]


# ----------------------------------------------------------------------------------
# L2: a tree dialog accepted after an edit was applied
# ----------------------------------------------------------------------------------
def accept_json(qtbot, dlg, text: str) -> None:
    qtbot.waitUntil(lambda: not dlg.loading, timeout=TIMEOUT)
    dlg.editor.setPlainText(text)
    qtbot.waitUntil(lambda: dlg.ok_button.isEnabled() and dlg.is_valid(), timeout=TIMEOUT)
    dlg.accept()


def test_tree_dialog_follows_its_node_through_other_edits(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(5)))
    view = views(TreeView, s)
    tree_select(qtbot, view, (2,))
    press(view.grid, K.Key_Return, M.ShiftModifier)
    dlg = visible_dialog(qtbot, view, editors.JsonEditDialog)
    qtbot.waitUntil(lambda: not dlg.loading, timeout=TIMEOUT)
    with applied(qtbot, s):                               # meanwhile: record 0 deleted -> the node is #1 now
        s.edit.delete([NodeRef(0, (), ())])
    with applied(qtbot, s):
        accept_json(qtbot, dlg, '{"id": 2, "name": "FROM_DIALOG"}')
    assert [r["name"] for r in records(s)] == ["n1", "FROM_DIALOG", "n3", "n4"]


def test_tree_dialog_of_a_deleted_node_says_so(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(4)))
    view = views(TreeView, s)
    tree_expand(qtbot, view, (1,))
    tree_select(qtbot, view, (1, 1))                      # "name"
    press(view.grid, K.Key_Return, M.ShiftModifier)
    dlg = visible_dialog(qtbot, view, editors.JsonEditDialog)
    qtbot.waitUntil(lambda: not dlg.loading, timeout=TIMEOUT)
    with applied(qtbot, s):
        s.edit.delete([NodeRef(1, (), ())])               # the record holding the node
    steps = s.edit.steps
    accept_json(qtbot, dlg, '"FROM_DIALOG"')
    settle(qtbot, app)
    assert s.edit.steps == steps and not s.edit.busy
    assert app.statuses and app.statuses[-1] == t("gui.tree.edit.gone")
    assert "FROM_DIALOG" not in str(records(s))


def test_tree_insert_dialog_follows_its_place(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(4)))
    view = views(TreeView, s)
    tree_select(qtbot, view, (2,))
    dlg = view.insert_node("after")                       # a new record after #2
    dlg.set_value_type("number")
    dlg.number_edit.setText("77")
    with applied(qtbot, s):
        s.edit.delete([NodeRef(0, (), ())])               # #2 is #1 now
    with applied(qtbot, s):
        dlg.accept()
    assert [r if isinstance(r, int) else r["name"] for r in records(s)] == ["n1", "n2", 77, "n3"]


# ----------------------------------------------------------------------------------
# L1 in the table
# ----------------------------------------------------------------------------------
def table_view(qtbot, views, s, columns: list[str]) -> TableView:
    view = views(TableView, s)
    view.set_columns(columns)
    qtbot.waitUntil(lambda: all(view.cell_text(r, columns[0]) is not None for r in range(min(3, s.view_len()))),
                    timeout=TIMEOUT)
    return view


def table_current(view: TableView, row: int, path: str) -> None:
    view.grid.setFocus()
    view.grid.set_current(row, next(i for i, c in enumerate(view.grid.columns()) if c.id == path))


def test_table_record_key_editor_refuses_existing_keys(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(keyed_file(tmp_path, 2000), format="keyed")
    view = table_view(qtbot, views, s, ["$key", "v"])
    table_current(view, 1, "$key")
    press(view.grid, K.Key_F2)
    qtbot.waitUntil(lambda: view.inline_editor() is not None, timeout=TIMEOUT)
    ed = view.inline_editor()
    ed.setText("k2")                                       # a loaded row has it: refused at once
    assert not ed.is_valid() and ed.commit() is False
    ed.setText("k1999")                                    # a record far below (not loaded): looked up
    assert ed.is_valid() and ed.commit() is False and ed.checking
    qtbot.waitUntil(lambda: not ed.checking, timeout=TIMEOUT)
    settle(qtbot, app)
    assert not ed.done and not ed.is_valid() and "k1999" in ed.error_text()
    assert s.edit.steps == 0 and not app.errors
    with applied(qtbot, s):
        ed.setText("yangi")
        ed.commit()
    assert s.doc.pinned().record_key(1) == "yangi"


def test_table_insert_record_refuses_an_existing_key(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(keyed_file(tmp_path, 4), format="keyed")
    view = table_view(qtbot, views, s, ["$key", "v"])
    table_current(view, 1, "v")
    dlg = view.insert_record()
    assert dlg.key_needed()
    dlg.key_edit.setText("k3")
    dlg.set_value_type("number")
    dlg.number_edit.setText("5")
    if dlg.ok_button.isEnabled():                          # not decided at once: looked up on OK
        dlg.accept()
        qtbot.waitUntil(lambda: not dlg.checking, timeout=TIMEOUT)
    settle(qtbot, app)
    assert dlg.isVisible() and t("gui.editors.error.key_exists", key="k3") in dlg.error_label.text()
    assert s.edit.steps == 0 and not app.errors
    with applied(qtbot, s):
        dlg.key_edit.setText("k9")
        dlg.accept()
    assert [s.doc.pinned().record_key(i) for i in range(5)] == ["k0", "k1", "k9", "k2", "k3"]


def test_table_member_rename_and_insert_refuse_sibling_keys(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(3)))
    view = table_view(qtbot, views, s, ["id", "city", "contact"])
    table_current(view, 1, "city")
    press(view.grid, K.Key_F2)                             # F2 renames the member (review3 L5)
    dlg = view.edit_dialog()
    assert isinstance(dlg, editors.KeyDialog) and dlg.edit.text() == "city"
    dlg.edit.setText("name")                               # a sibling in the kept record value
    assert not dlg.ok_button.isEnabled() and "name" in dlg.error_label.text()
    dlg.accept()
    settle(qtbot, app)
    assert dlg.isVisible() and s.edit.steps == 0 and not app.errors
    dlg.discard()
    table_current(view, 1, "contact")                      # an object cell: a new member inside
    ins = view.insert_node("child")
    ins.key_edit.setText("zip")
    assert not ins.ok_button.isEnabled() and "zip" in ins.error_label.text()
    with applied(qtbot, s):
        ins.key_edit.setText("email")
        ins.accept()
    assert records(s)[1]["contact"] == {"phone": "+9980000001", "zip": 100001, "email": ""}


def test_table_selection_made_by_the_user_wins_over_a_follow_on_its_way(qtbot, app, opened, views,
                                                                        tmp_path) -> None:
    """Another view's selection is followed after a position lookup; a cell the user made current meanwhile
    stays current (a value load waiting for it would otherwise be dropped)."""
    s = opened(write_json(tmp_path / "p.json", people(6)))
    view = table_view(qtbot, views, s, ["id", "name", "city"])
    s.select(NodeRef(4, ("name",)))                        # followed asynchronously
    table_current(view, 2, "city")                         # ... while the user moves
    settle(qtbot, app)
    assert view.grid.current() == (2, next(i for i, c in enumerate(view.grid.columns()) if c.id == "city"))


# ----------------------------------------------------------------------------------
# L3: "change type" offers no conversion §12.4 always refuses
# ----------------------------------------------------------------------------------
def menu_actions(menu: QMenu) -> dict:
    from PySide6.QtGui import QAction

    return {a.objectName(): a for a in menu.findChildren(QAction) if a.objectName()}


def test_tree_type_menu_follows_the_conversion_rules(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(3)))
    view = views(TreeView, s)
    tree_expand(qtbot, view, (0,))
    row = tree_select(qtbot, view, (0, 4))                # contact: an object
    acts = menu_actions(view.context_menu(row))
    for kind in ("number", "bool"):
        assert not acts[f"edit_type_{kind}"].isEnabled(), kind
        assert acts[f"edit_type_{kind}"].toolTip() == editors.type_refusal("object", kind)
    assert not acts["edit_type_object"].isEnabled()       # already an object
    assert acts["edit_type_string"].isEnabled() and acts["edit_type_array"].isEnabled()
    row = tree_select(qtbot, view, (0, 0))                # id: a number
    acts = menu_actions(view.context_menu(row))
    assert not acts["edit_type_object"].isEnabled() and acts["edit_type_array"].isEnabled()
    assert acts["edit_type_string"].isEnabled() and acts["edit_type_bool"].isEnabled()
    view.select_keypaths([(0, 0), (0, 3)])                # a number and an array together
    acts = menu_actions(view.context_menu(view.row_of((0, 0))))
    assert not acts["edit_type_object"].isEnabled() and not acts["edit_type_number"].isEnabled()
    assert acts["edit_type_string"].isEnabled() and acts["edit_type_null"].isEnabled()


def test_table_type_menu_follows_the_conversion_rules(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(3)))
    view = table_view(qtbot, views, s, ["id", "contact", "tags"])
    qtbot.waitUntil(lambda: view.cell_text(0, "contact") is not None, timeout=TIMEOUT)
    table_current(view, 0, "contact")
    acts = menu_actions(view.cell_menu(0, next(i for i, c in enumerate(view.grid.columns()) if c.id == "contact")))
    assert not acts["edit_type_number"].isEnabled() and not acts["edit_type_bool"].isEnabled()
    assert acts["edit_type_array"].isEnabled() and acts["edit_type_string"].isEnabled()
    table_current(view, 0, "id")
    acts = menu_actions(view.cell_menu(0, 0))
    assert not acts["edit_type_object"].isEnabled() and acts["edit_type_string"].isEnabled()


# ----------------------------------------------------------------------------------
# L5: F2 renames in the table; the menus show each view's keys
# ----------------------------------------------------------------------------------
def test_edit_keys_shown_per_view(qtbot, app, opened, views, tmp_path) -> None:
    from jsonreshape.gui import actions

    s = opened(write_json(tmp_path / "p.json", people(3)))
    tree = views(TreeView, s)
    table = table_view(qtbot, views, s, ["id", "name"])
    assert actions.command_keys(tree, "move_up") == "Alt+Up" and actions.command_keys(tree, "rename") == "F2"
    assert actions.command_keys(table, "move_up") is None and actions.command_keys(table, "move_down") is None
    assert actions.command_keys(table, "rename") == "F2" and actions.command_keys(None, "delete") == "Del"
    table_current(table, 0, "name")
    acts = menu_actions(table.cell_menu(0, 1))
    assert "\t" not in acts["edit_move_up"].text() and "\t" not in acts["edit_move_down"].text()
    assert acts["edit_rename"].text().endswith("\tF2")
    row = tree_select(qtbot, tree, (0,))
    acts = menu_actions(tree.context_menu(row))
    assert acts["edit_move_up"].text().endswith("\t" + actions.with_keys("", "Alt+Up").strip("\t"))
    # F2 on a value cell renames the member, Enter edits the value
    table_current(table, 1, "name")
    press(table.grid, K.Key_F2)
    dlg = table.edit_dialog()
    assert isinstance(dlg, editors.KeyDialog) and dlg.edit.text() == "name"
    dlg.discard()
    press(table.grid, K.Key_Return)
    qtbot.waitUntil(lambda: table.inline_editor() is not None, timeout=TIMEOUT)
    assert table.inline_editor().kind == "string" and table.inline_editor().text() == "n1"
    table.inline_editor().cancel()


# ----------------------------------------------------------------------------------
# L7: protected rows
# ----------------------------------------------------------------------------------
def wrapped_file(tmp_path: Path) -> Path:
    return write_json(tmp_path / "w.json", {"meta": {"n": 1}, "data": people(3)})


def test_protected_rows_say_why_and_enter_toggles_the_wrapper(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(wrapped_file(tmp_path), format="wrapped", record_path="data")
    view = views(TreeView, s)
    tree_select(qtbot, view, ("wrapper",))
    for key, mods in ((K.Key_Delete, M.NoModifier), (K.Key_D, M.ControlModifier), (K.Key_Down, M.AltModifier)):
        app.statuses.clear()
        press(view.grid, key, mods)
        assert app.statuses == [t("gui.tree.edit.fixed.wrapper")], (key, app.statuses)
    assert view.edit_command_state("delete") == (False, t("gui.tree.edit.fixed.wrapper"))
    assert not view.flat.is_expanded(("wrapper",))
    press(view.grid, K.Key_Return)                         # Enter toggles the wrapper object
    qtbot.waitUntil(lambda: view.flat.is_expanded(("wrapper",)), timeout=TIMEOUT)
    tree_select(qtbot, view, ("wrapper", 1))               # the record container
    for key, mods in ((K.Key_Delete, M.NoModifier), (K.Key_D, M.ControlModifier)):
        app.statuses.clear()
        press(view.grid, key, mods)
        assert app.statuses == [t("gui.tree.edit.fixed.records")], (key, app.statuses)
    press(view.grid, K.Key_Return)                         # nothing to toggle: says why (records are below)
    assert app.statuses[-1] == t("gui.tree.edit.records_locked")
    tree_select(qtbot, view, ("wrapper",))
    press(view.grid, K.Key_Return)
    qtbot.waitUntil(lambda: not view.flat.is_expanded(("wrapper",)), timeout=TIMEOUT)
    assert s.edit.steps == 0 and not app.errors


def test_single_document_root_says_why(qtbot, app, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "one.json", {"a": 1, "b": [1, 2]}), format="single")
    view = views(TreeView, s)
    tree_select(qtbot, view, ("root",))
    for key, mods in ((K.Key_Delete, M.NoModifier), (K.Key_D, M.ControlModifier), (K.Key_Up, M.AltModifier)):
        app.statuses.clear()
        press(view.grid, key, mods)
        assert app.statuses == [t("gui.tree.edit.fixed.root")], (key, app.statuses)
    assert s.edit.steps == 0 and not app.errors
