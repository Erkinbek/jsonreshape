"""Tree editing end to end (jsonreshape/gui/treeview.py; docs/ARCHITECTURE.md §12.9 "Tree", "Keys",
"Editing values").

Real sessions with a real :class:`~jsonreshape.gui.editing.EditController`: Enter (inline editor for short
strings and numbers -- exact tokens --, a dialog for long strings, bool toggle, the JSON editor for null and
containers), Shift+Enter, F2 (member keys, keyed records), Insert (menu + dialog: child / before / after, new
records), Del / Ctrl+D on multi-selections (also record ranges beyond ``EDIT_SELECTION_MAX`` through the record
commands), Ctrl+V, Alt+Up/Down, change type; inline commit / cancel; command states and their reasons; the
behaviour without editing (Enter toggles). Every change is checked in the effective document and in the rows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from jsonreshape.gui import editors, treeview, widgets  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.session import DocSession, NodeRef  # noqa: E402
from jsonreshape.gui.treemodel import ROOT, WRAPPER  # noqa: E402
from jsonreshape.gui.treeview import TreeView  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000
K = Qt.Key
M = Qt.KeyboardModifier
LEFT = Qt.MouseButton.LeftButton


# ----------------------------------------------------------------------------------
# fixtures / helpers
# ----------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_modal_confirm(monkeypatch):
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: True)


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.statuses = []
    ctx.error_reported.connect(ctx.errors.append)
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def opened(qtbot, app):
    made: list[DocSession] = []

    def open_file(path: Path, *, editable: bool = True, **kw) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
            s.start()
        if editable:
            qtbot.waitUntil(lambda: s.edit is not None and s.edit.available, timeout=TIMEOUT)
        return s

    yield open_file
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)
    assert not app.errors, app.errors


@pytest.fixture
def views(qtbot):
    made: list[TreeView] = []

    def make(session: DocSession) -> TreeView:
        view = TreeView(session)
        qtbot.addWidget(view)
        view.resize(1000, 600)
        view.show()
        qtbot.waitExposed(view)
        view.activate()
        made.append(view)
        return view

    yield make
    for v in made:
        v.shutdown()


def write(path: Path, value, *, indent=None, newline: str = "\n") -> Path:
    """``value`` as JSON (UTF-8, ``newline`` line ends); :class:`Exact` floats keep their token."""
    exact: dict[str, str] = {}

    def mark(v):
        if isinstance(v, Exact):
            marker = f"@@exact{len(exact)}@@"
            exact[f'"{marker}"'] = v.token
            return marker
        if isinstance(v, dict):
            return {k: mark(x) for k, x in v.items()}
        if isinstance(v, list):
            return [mark(x) for x in v]
        return v

    text = json.dumps(mark(value), ensure_ascii=False, indent=indent).replace("\n", newline)
    for marker, token in exact.items():
        text = text.replace(marker, token)
    path.write_bytes(text.encode("utf-8"))
    return path


def wait_text(qtbot, view: TreeView, row: int, col: str = "key", text: str | None = None) -> str:
    def ok() -> bool:
        if row >= view.row_count():
            return False
        got = view.cell_text(row, col)
        return got is not None and (text is None or got == text)

    qtbot.waitUntil(ok, timeout=TIMEOUT)
    return view.cell_text(row, col)


def wait_idle(qtbot, view: TreeView) -> None:
    qtbot.waitUntil(lambda: not view.busy(), timeout=TIMEOUT)


def expand(qtbot, view: TreeView, kp: tuple) -> None:
    qtbot.waitUntil(lambda: view.row_of(kp) is not None, timeout=TIMEOUT)
    wait_text(qtbot, view, view.row_of(kp))
    view.expand_node(kp)
    qtbot.waitUntil(lambda: view.flat.is_expanded(kp) and not view.is_loading(kp), timeout=TIMEOUT)
    wait_idle(qtbot, view)


def select(qtbot, view: TreeView, kp: tuple) -> int:
    qtbot.waitUntil(lambda: view.row_of(kp) is not None, timeout=TIMEOUT)
    row = view.row_of(kp)
    wait_text(qtbot, view, row)
    view.select_row(row, scroll="ensure")
    qtbot.waitUntil(lambda: view.current_keypath() == kp, timeout=TIMEOUT)
    return row


def key(qtbot, view: TreeView, k, mods=M.NoModifier) -> None:
    view.grid.setFocus()
    QTest.keyClick(view.grid, k, mods)


def changed(qtbot, s: DocSession, fn):
    """Run ``fn`` and wait for the ChangeSet it causes (and until the controller takes the next command:
    one command in flight)."""
    with qtbot.waitSignal(s.data_changed, timeout=TIMEOUT) as blocker:
        fn()
    qtbot.waitUntil(lambda: s.edit is None or not s.edit.busy, timeout=TIMEOUT)
    return blocker.args[0]


def values(s: DocSession) -> list:
    v = s.doc.pinned()
    return [v.record(i) for i in range(v.record_count)]


def raw(s: DocSession, pos: int) -> bytes:
    return s.doc.pinned().record_bytes(pos)


def editor(qtbot, view: TreeView):
    qtbot.waitUntil(lambda: view.inline_editor() is not None, timeout=TIMEOUT)
    return view.inline_editor()


def dialog(qtbot, view: TreeView, cls):
    def find():
        return [d for d in view.findChildren(cls) if d.isVisible()]

    qtbot.waitUntil(lambda: bool(find()), timeout=TIMEOUT)
    return find()[-1]


def accept_text(qtbot, dlg, text: str) -> None:
    """Type ``text`` into a loaded dialog (as the user would) and press OK once it is valid."""
    qtbot.waitUntil(lambda: not dlg.loading, timeout=TIMEOUT)
    dlg.editor.setPlainText(text)
    qtbot.waitUntil(lambda: dlg.ok_button.isEnabled() and (not hasattr(dlg, "is_valid") or dlg.is_valid()),
                    timeout=TIMEOUT)
    dlg.accept()


class Exact(float):
    """A float written with an exact token by :func:`write` (``1.10`` stays ``1.10``)."""

    def __new__(cls, token: str) -> "Exact":
        obj = super().__new__(cls, token)
        obj.token = token
        return obj


def people(n: int) -> list[dict]:
    return [{"id": i, "name": f"user{i}", "price": Exact("1.10") if i == 0 else i, "ok": i % 2 == 0,
             "none": None, "tags": ["a", "b"], "o": {"x": i, "y": "O'zbekiston"}} for i in range(n)]


PEOPLE_KEYS = ["id", "name", "price", "ok", "none", "tags", "o"]


def kp_of(name: str, record: int = 0) -> tuple:
    return (record, PEOPLE_KEYS.index(name))


# ----------------------------------------------------------------------------------
# Enter: values
# ----------------------------------------------------------------------------------
def test_enter_edits_a_string_inline_commit_and_cancel(qtbot, app, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(3), indent=2))
    view = views(s)
    expand(qtbot, view, (0,))
    select(qtbot, view, kp_of("name"))
    key(qtbot, view, K.Key_Return)
    ed = editor(qtbot, view)
    assert ed.text() == "user0" and ed.kind == "string"
    ed.setText("Тошкент ва O'zbekiston \"q\"")
    cs = changed(qtbot, s, lambda: QTest.keyClick(ed, K.Key_Return))
    assert cs.records == frozenset({0}) and view.inline_editor() is None
    assert values(s)[0]["name"] == "Тошкент ва O'zbekiston \"q\""
    row = view.row_of(kp_of("name"))
    wait_text(qtbot, view, row, "value", '"Тошкент ва O\'zbekiston \\"q\\""')
    # the record keeps its other bytes (indentation of the pretty file)
    assert b'\n    "id": 0,\n    "name": "' in raw(s, 0)
    # Esc cancels: no step
    steps = s.edit.steps
    key(qtbot, view, K.Key_Return)
    ed = editor(qtbot, view)
    ed.setText("nope")
    QTest.keyClick(ed, K.Key_Escape)
    qtbot.waitUntil(lambda: view.inline_editor() is None, timeout=TIMEOUT)
    app.tasks.wait_idle(5)
    assert s.edit.steps == steps and values(s)[0]["name"].startswith("Тошкент")
    assert view.current_keypath() == kp_of("name")


def test_enter_number_keeps_the_exact_token(qtbot, opened, views, tmp_path: Path) -> None:
    path = tmp_path / "n.json"
    path.write_text('[{"price": 1.10, "big": 12345678901234567890, "neg": -0.0, "e": 1e400}]', encoding="utf-8")
    s = opened(path)
    view = views(s)
    expand(qtbot, view, (0,))
    select(qtbot, view, (0, 0))
    key(qtbot, view, K.Key_Return)
    ed = editor(qtbot, view)
    assert ed.text() == "1.10" and ed.kind == "number"
    ed.setText("12345678901234567890.000")
    changed(qtbot, s, lambda: QTest.keyClick(ed, K.Key_Return))
    assert b'"price": 12345678901234567890.000' in raw(s, 0)
    for kp, token in (((0, 1), "12345678901234567890"), ((0, 2), "-0.0"), ((0, 3), "1e400")):
        select(qtbot, view, kp)
        key(qtbot, view, K.Key_Return)
        ed = editor(qtbot, view)
        assert ed.text() == token
        QTest.keyClick(ed, K.Key_Escape)
        qtbot.waitUntil(lambda: view.inline_editor() is None, timeout=TIMEOUT)


def test_bool_toggles_and_null_opens_the_json_editor(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(2)))
    view = views(s)
    expand(qtbot, view, (0,))
    select(qtbot, view, kp_of("ok"))
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_Return))
    assert values(s)[0]["ok"] is False
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_Return))
    assert values(s)[0]["ok"] is True
    select(qtbot, view, kp_of("none"))
    key(qtbot, view, K.Key_Return)
    dlg = dialog(qtbot, view, editors.JsonEditDialog)
    qtbot.waitUntil(lambda: not dlg.loading, timeout=TIMEOUT)
    assert dlg.text() == "null"
    changed(qtbot, s, lambda: accept_text(qtbot, dlg, '{"a": [1, 2.50]}'))
    assert values(s)[0]["none"] == {"a": [1, 2.50]}
    assert b'2.50' in raw(s, 0)
    # the edited node became a container: it can be expanded now
    expand(qtbot, view, kp_of("none"))
    assert view.flat.child_count(kp_of("none")) == 1


def test_container_and_shift_enter_open_the_json_editor(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(2)))
    view = views(s)
    expand(qtbot, view, (0,))
    expand(qtbot, view, kp_of("o"))
    select(qtbot, view, kp_of("o"))
    key(qtbot, view, K.Key_Return)                         # a container: the JSON editor
    dlg = dialog(qtbot, view, editors.JsonEditDialog)
    qtbot.waitUntil(lambda: not dlg.loading, timeout=TIMEOUT)
    assert json.loads(dlg.text()) == {"x": 0, "y": "O'zbekiston"}
    cs = changed(qtbot, s, lambda: accept_text(qtbot, dlg, '{"x": 0, "y": "Samarqand", "z": [true]}'))
    assert cs.nodes and cs.nodes[0].count == 3                   # all old children replaced
    assert values(s)[0]["o"] == {"x": 0, "y": "Samarqand", "z": [True]}
    assert view.flat.is_expanded(kp_of("o")) and view.flat.child_count(kp_of("o")) == 3
    # Shift+Enter on a scalar: the JSON editor with its token
    select(qtbot, view, kp_of("name"))
    key(qtbot, view, K.Key_Return, M.ShiftModifier)
    dlg = dialog(qtbot, view, editors.JsonEditDialog)
    qtbot.waitUntil(lambda: not dlg.loading, timeout=TIMEOUT)
    assert dlg.text() == '"user0"'
    changed(qtbot, s, lambda: accept_text(qtbot, dlg, "[1, 2]"))
    assert values(s)[0]["name"] == [1, 2]


def test_long_and_multiline_strings_use_the_string_dialog(qtbot, opened, views, tmp_path: Path) -> None:
    long = "Toshkent " * 40
    s = opened(write(tmp_path / "l.json", [{"long": long, "multi": "a\nb", "huge": "x" * 70_000}]))
    view = views(s)
    expand(qtbot, view, (0,))
    for kp, start in (((0, 0), long), ((0, 1), "a\nb")):
        select(qtbot, view, kp)
        key(qtbot, view, K.Key_Return)
        dlg = dialog(qtbot, view, editors.StringDialog)
        qtbot.waitUntil(lambda: not dlg.loading, timeout=TIMEOUT)
        assert dlg.value() == start
        assert view.inline_editor() is None
        dlg.discard()
    # above the line loader's 64 KiB: the dialog reads the string itself
    select(qtbot, view, (0, 2))
    key(qtbot, view, K.Key_Return)
    dlg = dialog(qtbot, view, editors.StringDialog)
    qtbot.waitUntil(lambda: not dlg.loading and dlg.value() == "x" * 70_000, timeout=TIMEOUT)
    changed(qtbot, s, lambda: accept_text(qtbot, dlg, "short\nnow"))
    assert values(s)[0]["huge"] == "short\nnow"


def test_a_key_waits_for_the_rows_data(qtbot, opened, views, tmp_path: Path) -> None:
    """Right after an edit the current row's data is read again: Enter / F2 pressed meanwhile act
    once it arrived (on the same node), they are not lost."""
    s = opened(write(tmp_path / "p.json", people(2)))
    view = views(s)
    expand(qtbot, view, (0,))
    select(qtbot, view, kp_of("ok"))
    view._loader.invalidate()
    view._known.clear()
    assert view.info_at(view.row_of(kp_of("ok"))) is None
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_Return))       # waits, then toggles
    assert values(s)[0]["ok"] is False
    select(qtbot, view, kp_of("name"))
    view._loader.invalidate()
    view._known.clear()
    key(qtbot, view, K.Key_F2)
    ed = editor(qtbot, view)
    assert ed.kind == "key" and ed.text() == "name"
    QTest.keyClick(ed, K.Key_Escape)
    # moved on before the data came: dropped
    view._loader.invalidate()
    view._known.clear()
    key(qtbot, view, K.Key_F2)
    assert view._pending_command is not None
    view.select_row(view.row_of(kp_of("id")))                    # at once, before the data arrives
    wait_idle(qtbot, view)
    wait_text(qtbot, view, view.row_of(kp_of("name")))
    assert view.inline_editor() is None and view._pending_command is None


def test_double_click_edits_scalars_and_toggles_containers(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(2)))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 2, timeout=TIMEOUT)
    wait_text(qtbot, view, 0)
    grid = view.grid
    QTest.mouseDClick(grid.viewport(), LEFT, M.NoModifier, grid.cell_rect(0, 1).center())
    qtbot.waitUntil(lambda: view.flat.is_expanded((0,)), timeout=TIMEOUT)
    row = view.row_of(kp_of("id"))
    wait_text(qtbot, view, row)
    QTest.mouseDClick(grid.viewport(), LEFT, M.NoModifier, grid.cell_rect(row, 1).center())
    ed = editor(qtbot, view)
    assert ed.text() == "0"
    ed.setText("7")
    changed(qtbot, s, lambda: QTest.keyClick(ed, K.Key_Return))
    assert values(s)[0]["id"] == 7


# ----------------------------------------------------------------------------------
# F2
# ----------------------------------------------------------------------------------
def test_f2_renames_members_and_keyed_records(qtbot, app, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(2)))
    view = views(s)
    expand(qtbot, view, (0,))
    select(qtbot, view, kp_of("name"))
    key(qtbot, view, K.Key_F2)
    ed = editor(qtbot, view)
    assert ed.kind == "key" and ed.text() == "name"
    ed.setText("ism")
    cs = changed(qtbot, s, lambda: QTest.keyClick(ed, K.Key_Return))
    assert not cs.nodes and list(values(s)[0]) == ["id", "ism", "price", "ok", "none", "tags", "o"]
    wait_text(qtbot, view, view.row_of(kp_of("name")), "key", "ism")     # same position, new key
    # an array element has no key
    expand(qtbot, view, kp_of("tags"))
    select(qtbot, view, kp_of("tags") + (0,))
    key(qtbot, view, K.Key_F2)
    assert view.inline_editor() is None and app.statuses[-1] == treeview.t("gui.tree.edit.no_key")
    assert view.edit_command_state("rename")[0] is False
    # keyed documents: the record key
    s2 = opened(write(tmp_path / "k.json", {"k0": {"a": 1}, "k1": {"a": 2}, "k2": {"a": 3}}), format="keyed")
    v2 = views(s2)
    select(qtbot, v2, (1,))
    key(qtbot, v2, K.Key_F2)
    ed = editor(qtbot, v2)
    assert ed.text() == "k1"
    ed.setText("Ўзбек")
    changed(qtbot, s2, lambda: QTest.keyClick(ed, K.Key_Return))
    assert s2.doc.pinned().record_key(1) == "Ўзбек"
    wait_text(qtbot, v2, 1, "key", "Ўзбек")


# ----------------------------------------------------------------------------------
# Insert
# ----------------------------------------------------------------------------------
def test_insert_menu_and_dialog(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(3)))
    view = views(s)
    expand(qtbot, view, (0,))
    expand(qtbot, view, kp_of("tags"))
    select(qtbot, view, kp_of("tags"))
    menu = view.insert_menu()
    names = {a.objectName(): a for a in menu.actions()}
    assert set(names) == {"tree_insert_child", "tree_insert_before", "tree_insert_after"}
    assert all(a.isEnabled() for a in names.values())
    names["tree_insert_child"].trigger()
    dlg = dialog(qtbot, view, editors.InsertDialog)
    assert dlg.position() == "child" and set(dlg.positions) == {"before", "after", "child"}
    dlg.string_edit.setText("c")
    cs = changed(qtbot, s, dlg.accept)
    assert values(s)[0]["tags"] == ["a", "b", "c"]
    assert cs.select is not None and view.current_keypath() == kp_of("tags") + (2,)
    # before a member of an object: the key is asked
    select(qtbot, view, kp_of("price"))
    dlg = view.insert_node("before")
    assert dlg.key_needed("before")
    dlg.key_edit.setText("yangi")
    dlg.type_combo.setCurrentIndex(dlg.type_combo.findData("number"))
    dlg.number_edit.setText("1.50")
    changed(qtbot, s, dlg.accept)
    assert list(values(s)[0])[:4] == ["id", "name", "yangi", "price"]
    assert b'"yangi": 1.50' in raw(s, 0)
    # the expansion of tags (now one place further) stays
    assert view.flat.is_expanded((0, 6)) and view.current_keypath() == (0, 2)
    # a new record after record 1
    select(qtbot, view, (1,))
    menu = view.insert_menu()
    assert {a.text() for a in menu.actions()} >= {treeview.t("gui.tree.edit.insert.record_after")}
    dlg = view.insert_node("after")
    dlg.type_combo.setCurrentIndex(dlg.type_combo.findData("object"))
    changed(qtbot, s, dlg.accept)
    assert values(s)[2] == {} and len(values(s)) == 4
    assert view.current_keypath() == (2,) and view.row_count() == 4 + view.flat.child_count((0,)) + 3


def test_insert_states(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(2)))
    view = views(s)
    expand(qtbot, view, (0,))
    select(qtbot, view, kp_of("id"))
    assert view.edit_command_state("insert_child")[0] is False
    assert view.edit_command_state("insert_before") == (True, "")
    menu = view.insert_menu()
    child = next(a for a in menu.actions() if a.objectName() == "tree_insert_child")
    assert not child.isEnabled() and child.toolTip()
    single = opened(write(tmp_path / "s.json", {"a": 1}), format="single")
    v2 = views(single)
    qtbot.waitUntil(lambda: v2.row_count() >= 1, timeout=TIMEOUT)
    select(qtbot, v2, (ROOT,))
    assert v2.edit_command_state("insert_before")[0] is False
    assert v2.edit_command_state("insert_child") == (True, "")


# ----------------------------------------------------------------------------------
# Del / Ctrl+D / Ctrl+V / Alt+arrows / change type
# ----------------------------------------------------------------------------------
def test_delete_multi_selection(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(5)))
    view = views(s)
    expand(qtbot, view, (0,))
    expand(qtbot, view, (3,))
    expand(qtbot, view, (3, 6))                         # o of record 3
    view.select_keypaths([kp_of("name"), kp_of("ok"), (1,), (0, 5)])
    assert view.selection_count() == 4
    assert view.selected_keypaths() == [kp_of("name"), kp_of("ok"), kp_of("tags"), (1,)]
    cs = changed(qtbot, s, lambda: key(qtbot, view, K.Key_Delete))
    assert cs.remap is not None
    vals = values(s)
    assert len(vals) == 4 and list(vals[0]) == ["id", "price", "none", "o"] and vals[1]["id"] == 2
    # record 3 is record 2 now: its expansion followed
    assert view.flat.is_expanded((2,)) and view.flat.is_expanded((2, 6))
    assert not view.flat.is_expanded((3,))
    assert view.current_keypath() is not None and view.selection_count() == 1


def test_duplicate_and_paste(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(3)))
    view = views(s)
    expand(qtbot, view, (0,))
    select(qtbot, view, kp_of("tags"))
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_D, M.ControlModifier))
    keys = list(values(s)[0])
    assert keys[6] == "tags_2" and values(s)[0]["tags_2"] == ["a", "b"]
    assert view.current_keypath() == (0, 6)                     # the copy is selected
    # Ctrl+V: JSON from the clipboard as a child of a container ...
    QGuiApplication.clipboard().setText('{"p": [1, 2.0]}')
    select(qtbot, view, (0, 7))                                 # "o", one place further now
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_V, M.ControlModifier))
    assert values(s)[0]["o"]["pasted"] == {"p": [1, 2.0]}
    # ... or after a scalar
    QGuiApplication.clipboard().setText("42")
    select(qtbot, view, kp_of("id"))
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_Insert, M.ShiftModifier))
    assert list(values(s)[0])[:2] == ["id", "pasted"] and values(s)[0]["pasted"] == 42
    # record rows duplicate whole records
    view.select_keypaths([(1,), (2,)])
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_D, M.ControlModifier))
    assert [r["id"] for r in values(s)] == [0, 1, 1, 2, 2]
    QGuiApplication.clipboard().setText("   ")
    select(qtbot, view, (0,))
    key(qtbot, view, K.Key_V, M.ControlModifier)
    assert s.app.statuses[-1] == treeview.t("gui.tree.edit.clipboard_empty")


def test_alt_arrows_move_the_selection(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(4)))
    view = views(s)
    expand(qtbot, view, (0,))
    view.select_keypaths([kp_of("name"), kp_of("price")])
    for _ in range(2):
        changed(qtbot, s, lambda: key(qtbot, view, K.Key_Down, M.AltModifier))
    assert list(values(s)[0]) == ["id", "ok", "none", "name", "price", "tags", "o"]
    assert view.selected_keypaths() == [(0, 3), (0, 4)]                   # the block stays selected
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_Up, M.AltModifier))
    assert list(values(s)[0]) == ["id", "ok", "name", "price", "none", "tags", "o"]
    # records move in the base view
    expand(qtbot, view, (2,))
    select(qtbot, view, (2,))
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_Up, M.AltModifier))
    assert [r["id"] for r in values(s)] == [0, 2, 1, 3]
    assert view.flat.is_expanded((1,)) and not view.flat.is_expanded((2,))   # the expansion moved along
    # not siblings: refused with a message, nothing changes
    view.select_keypaths([(0, 1), (3,)])
    steps = s.edit.steps
    key(qtbot, view, K.Key_Down, M.AltModifier)
    assert s.app.statuses[-1] == treeview.t("gui.tree.edit.not_siblings") and s.edit.steps == steps
    assert view.edit_command_state("move_down") == (False, treeview.t("gui.tree.edit.not_siblings"))


def test_moves_refused_under_a_sorted_parent_and_in_filtered_views(qtbot, opened, views, tmp_path: Path) -> None:
    from jsonreshape.gui.session import FilterSpec

    s = opened(write(tmp_path / "p.json", people(6)))
    view = views(s)
    expand(qtbot, view, (0,))
    view.toggle_sort_keys(True)
    qtbot.waitUntil(lambda: view.flat.has_order((0,)), timeout=TIMEOUT)
    select(qtbot, view, kp_of("ok"))
    assert view.edit_command_state("move_up") == (False, treeview.t("gui.tree.edit.sorted_parent"))
    view.toggle_sort_keys(False)
    qtbot.waitUntil(lambda: not view.flat.has_order((0,)), timeout=TIMEOUT)
    assert view.edit_command_state("move_up") == (True, "")
    s.set_filter(FilterSpec("id >= 2"))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 4, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: view.row_count() >= 4, timeout=TIMEOUT)
    select(qtbot, view, (3,))
    assert view.edit_command_state("move_down") == (False, treeview.t("gui.tree.edit.view_records"))
    # node commands still work in a filtered view
    expand(qtbot, view, (3,))
    select(qtbot, view, (3, 1))
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_Delete))
    assert "name" not in values(s)[3]
    assert view.flat.is_expanded((3,))


def test_change_type_of_the_selection(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(2)))
    view = views(s)
    expand(qtbot, view, (0,))
    view.select_keypaths([kp_of("id"), kp_of("price")])
    changed(qtbot, s, lambda: view.change_type("string"))
    assert values(s)[0]["id"] == "0" and values(s)[0]["price"] == "1.10"    # the exact token as text


def test_big_record_selections_use_the_record_commands(qtbot, app, opened, views, tmp_path: Path,
                                                       monkeypatch) -> None:
    monkeypatch.setattr(treeview, "EDIT_SELECTION_MAX", 5)
    s = opened(write(tmp_path / "p.json", [{"id": i} for i in range(30)]))
    view = views(s)
    select(qtbot, view, (2,))
    view.grid.set_selected_ranges([(2, 11)])                     # 10 record rows > 5
    assert view.selected_keypaths() is None
    assert view.edit_command_state("delete") == (True, "")
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_Delete))
    assert [r["id"] for r in values(s)] == [0, 1] + list(range(12, 30))
    # duplicate them in a filtered view (positions read from the view's record set)
    from jsonreshape.gui.session import FilterSpec

    s.set_filter(FilterSpec("id >= 20"))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 10, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: view.row_count() == 10, timeout=TIMEOUT)
    wait_text(qtbot, view, 0)
    view.select_row(0)
    view.grid.select_all()
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_D, M.ControlModifier))
    ids = [r["id"] for r in values(s)]
    assert ids[-20:] == [i for i in range(20, 30) for _ in (0, 1)]
    # nested rows in a big selection: refused
    s.reset_view()
    qtbot.waitUntil(lambda: s.view_len() == len(ids), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: view.row_count() == len(ids), timeout=TIMEOUT)
    expand(qtbot, view, (1,))
    view.select_row(0)
    view.grid.set_selected_ranges([(0, 7)])
    steps = s.edit.steps
    key(qtbot, view, K.Key_Delete)
    assert app.statuses[-1] == treeview.t("gui.tree.edit.too_many", limit="5") and s.edit.steps == steps


# ----------------------------------------------------------------------------------
# the editor's value loader (treemodel, Qt-free)
# ----------------------------------------------------------------------------------
@pytest.fixture
def scanner_backend(core_backend):
    from jsonreshape import reader

    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    yield name
    reader.set_scanner_backend(old)


def test_edit_payload_reads_exact_tokens(tmp_path: Path, scanner_backend) -> None:
    from jsonreshape.edits import EditSession, Ref
    from jsonreshape.errors import TooLargeError
    from jsonreshape.gui.treemodel import NodeSource
    from jsonreshape.reader import Document

    path = tmp_path / "v.json"
    path.write_bytes(('[{"s": "O\'zbekiston \\u0442 \\"q\\" \\ud83d\\ude00", "n": 1.10, "big": 1e400, "z": -0.0, '
                      '"b": true, "nul": null, "o": {"a": 1}, "long": "' + "x" * 300 + '", "e": "\\ud800"}]')
                     .encode("utf-8"))
    doc = Document.open(path, use_cache=False)
    doc.build_index()
    try:
        src = NodeSource(doc.pinned())
        p = src.edit_payload((0, 0))
        assert (p.kind, p.value, p.key) == ("string", "O'zbekiston т \"q\" 😀", "s")
        assert p.token == '"O\'zbekiston \\u0442 \\"q\\" \\ud83d\\ude00"'
        assert src.edit_payload((0, 1)).token == "1.10"
        assert src.edit_payload((0, 2)).token == "1e400"
        assert src.edit_payload((0, 3)).token == "-0.0"
        assert src.edit_payload((0, 4)).token == "true"
        nul = src.edit_payload((0, 5))
        assert nul.kind == "null" and nul.token is None
        obj = src.edit_payload((0, 6))
        assert obj.kind == "object" and obj.token is None and obj.size == len(b'{"a": 1}')
        long = src.edit_payload((0, 7), token_max=100)
        assert long.kind == "string" and long.value is None and long.token is None    # the dialog's own loader
        assert src.edit_payload((0, 8)).value == "\ud800"                               # a lone surrogate
        with pytest.raises(TooLargeError):
            src.edit_payload((0, 1), token_max=2)
        # an edited document: the effective token
        es = EditSession.open(doc)
        es.set_value(Ref(0, ("n",), (1,)), "12345678901234567890.0")
        src2 = NodeSource(doc.pinned(), gen=1)
        assert src2.gen == 1 and src2.edit_payload((0, 1)).token == "12345678901234567890.0"
        assert src.edit_payload((0, 1)).token == "1.10"          # the old source still reads its state
        es.close()
    finally:
        doc.close()


# ----------------------------------------------------------------------------------
# context menu, bulk scope
# ----------------------------------------------------------------------------------
def test_context_menu_edit_group(qtbot, opened, views, tmp_path: Path) -> None:
    from PySide6.QtGui import QAction

    s = opened(write(tmp_path / "p.json", people(3)))
    view = views(s)
    expand(qtbot, view, (0,))
    expand(qtbot, view, kp_of("tags"))
    row = select(qtbot, view, kp_of("tags") + (1,))
    menu = view.context_menu(row)
    acts = {a.objectName(): a for a in menu.findChildren(QAction) if a.objectName()}
    assert acts["edit_edit_value"].isEnabled() and acts["edit_delete"].isEnabled()
    assert not acts["edit_rename"].isEnabled()
    assert acts["edit_rename"].toolTip() == treeview.t("gui.tree.edit.no_key")
    assert not acts["edit_insert_child"].isEnabled() and acts["edit_insert_after"].isEnabled()
    assert acts["tree_expand_all"] is not None                    # the tree's own items are still there
    changed(qtbot, s, acts["edit_delete"].trigger)
    assert values(s)[0]["tags"] == ["a"]
    menu.deleteLater()
    # change type through the menu's type submenu
    row = select(qtbot, view, kp_of("id"))
    menu = view.context_menu(row)
    acts = {a.objectName(): a for a in menu.findChildren(QAction) if a.objectName()}
    changed(qtbot, s, acts["edit_type_string"].trigger)
    assert values(s)[0]["id"] == "0"
    menu.deleteLater()


def test_bulk_selection_scope(qtbot, opened, views, tmp_path: Path, monkeypatch) -> None:
    s = opened(write(tmp_path / "p.json", people(12)))
    view = views(s)
    expand(qtbot, view, (2,))
    view.select_keypaths([(2, 1), (5,), (7,)])
    scope = view.bulk_selection()
    assert sorted(int(p) for chunk in scope.iter_chunks() for p in chunk) == [2, 5, 7]
    assert scope.gen == s.edit.applied_gen
    monkeypatch.setattr(treeview, "EDIT_SELECTION_MAX", 3)
    view.select_row(view.row_of((2, 3)))
    view.grid.set_selected_ranges([(view.row_of((2, 3)), view.row_of((6,)))])
    scope = view.bulk_selection()
    assert sorted({int(p) for chunk in scope.iter_chunks() for p in chunk}) == [2, 3, 4, 5, 6]


# ----------------------------------------------------------------------------------
# without editing
# ----------------------------------------------------------------------------------
def test_without_editing_enter_toggles_and_commands_explain(qtbot, app, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(2)), editable=False)
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 2, timeout=TIMEOUT)
    wait_text(qtbot, view, 0)
    real = s.edit
    s.edit = None
    try:
        select(qtbot, view, (0,))
        key(qtbot, view, K.Key_Return)
        assert view.flat.is_expanded((0,))                     # toggled like before editing existed
        state = view.edit_command_state("delete")
        assert state == (False, treeview.t("gui.tree.edit.unavailable"))
        key(qtbot, view, K.Key_Delete)
        assert app.statuses[-1] == treeview.t("gui.tree.edit.unavailable")
        drag_ok = view._drag_can_start([(0, 0)])
        assert drag_ok is False
    finally:
        s.edit = real


def test_wrapper_and_records_rows_are_protected(qtbot, opened, views, tmp_path: Path) -> None:
    data = {"meta": {"v": 1}, "data": [{"id": 0}, {"id": 1}], "tail": [1, 2]}
    s = opened(write(tmp_path / "w.json", data), format="wrapped", record_path="data")
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 3, timeout=TIMEOUT)
    expand(qtbot, view, (WRAPPER,))
    select(qtbot, view, (WRAPPER, 1))                       # the records member
    for name in ("edit_value", "edit_json", "rename", "insert_child"):
        assert view.edit_command_state(name)[0] is False, name
    view.select_keypaths([(WRAPPER,), (WRAPPER, 1)])
    assert view.selected_keypaths() == []
    # the wrapper's other members are editable
    expand(qtbot, view, (WRAPPER, 0))
    select(qtbot, view, (WRAPPER, 0, 0))
    key(qtbot, view, K.Key_Return)
    ed = editor(qtbot, view)
    ed.setText("2")
    cs = changed(qtbot, s, lambda: QTest.keyClick(ed, K.Key_Return))
    assert cs.wrapper and s.doc.pinned().root().value()["meta"] == {"v": 2}
    wait_text(qtbot, view, view.row_of((WRAPPER, 0, 0)), "value", "2")
    assert view.cell_text(view.row_of((WRAPPER, 0, 0)), "offset") == treeview.t("gui.tree.edited")
    # an inserted member of the wrapper object
    select(qtbot, view, (WRAPPER,))
    QGuiApplication.clipboard().setText('"x"')
    changed(qtbot, s, lambda: key(qtbot, view, K.Key_V, M.ControlModifier))
    assert s.doc.pinned().root().value()["pasted"] == "x"
    assert view.flat.is_expanded((WRAPPER,)) and view.flat.is_expanded((WRAPPER, 0))
