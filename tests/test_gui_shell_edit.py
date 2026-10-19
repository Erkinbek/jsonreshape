"""Editing in the application shell (ARCHITECTURE §12.9; AGENT_PROMPT §8, §11): the main
window's File / Edit / Navigate commands for editing, their shortcuts (window level for
Ctrl+S / Ctrl+Shift+S / Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z / Ctrl+H / Ctrl+B; the view keys
Enter, Shift+Enter, F2, Ins, Del, Ctrl+D, Ctrl+V, Alt+Up/Down are never shortcuts -- line
edits in the document area keep them), enabling from the edit controller, the ``*`` of
dirty titles, the close flows (tab / window / headless / after the event loop), Save As
(own format, another format offers the export), bulk / JSON Patch / discard, the node
menu's edit group and Ctrl+B.

The edit controller is a stub with the §12.9 surface (``guishell_helpers.StubEditController``,
installed instead of the real one when a session's index is ready); the views are stubs
too (an ``EditTreeStub`` implements the view edit protocol)."""

from __future__ import annotations

import types

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QLineEdit
from shiboken6 import isValid

from guishell_helpers import (
    TIMEOUT,
    EditTreeStub,
    StubBulkDialog,
    StubDialog,
    StubEditController,
    TextStub,
    edit_registry,
    install_stub_edit,
    make_app,
    records,
    stub_panels,
    wait_ready,
    write_json,
)
from jsonreshape.gui import actions, widgets
from jsonreshape.gui.actions import EDIT_COMMANDS, TYPE_KINDS, node_menu, with_keys
from jsonreshape.gui.doctab import DIRTY_MARK, DocumentTab
from jsonreshape.gui.mainwindow import MainWindow, save_as_formats, target_format
from jsonreshape.gui.session import NodeRef
from jsonreshape.i18n import MESSAGES, t

pytestmark = pytest.mark.gui

#: Keys the views' grids handle (§12.9): never a window / document-area shortcut.
VIEW_KEYS = ("Return", "Enter", "Shift+Return", "Shift+Enter", "F2", "Ins", "Del", "Ctrl+D", "Ctrl+V", "Alt+Up",
             "Alt+Down")
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier


# ----------------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------------
@pytest.fixture
def app(qapp, tmp_path, lang_uz):
    ctx = make_app(tmp_path)
    yield ctx
    for w in list(widgets._kept_alive):
        w.close()
    ctx.tasks.cancel_all()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def stubs(monkeypatch):
    StubEditController.instances.clear()
    EditTreeStub.states = {}
    StubBulkDialog.made.clear()
    StubDialog.made.clear()
    yield install_stub_edit(monkeypatch)
    EditTreeStub.states = {}


@pytest.fixture
def window(qtbot, app, stubs):
    w = MainWindow(app, view_registry=edit_registry(), panel_factories=stub_panels())
    qtbot.addWidget(w)
    yield w
    if not w._closing:
        w.close_without_questions()


def open_ready(qtbot, window: MainWindow, path) -> DocumentTab:
    tab = window.open_path(path)
    wait_ready(qtbot, tab.session)
    assert tab.session.state == "ready", tab.session.open_error
    qtbot.waitUntil(lambda: tab.edit is not None, timeout=TIMEOUT)
    return tab


@pytest.fixture
def data(tmp_path):
    return write_json(tmp_path / "a.json", records(30))


def activate(qtbot, window: MainWindow) -> None:
    window.show()
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window, timeout=TIMEOUT)


# ----------------------------------------------------------------------------------------
# shortcuts
# ----------------------------------------------------------------------------------------
def test_editing_shortcuts_are_window_level(window) -> None:
    expected = {"save": ["Ctrl+S"], "save_as": ["Ctrl+Shift+S"], "undo": ["Ctrl+Z"],
                "redo": ["Ctrl+Y", "Ctrl+Shift+Z"], "bulk": ["Ctrl+H"], "bookmark_add": ["Ctrl+B"]}
    for key, seqs in expected.items():
        action = window.action(key)
        assert [s.toString() for s in action.shortcuts()] == seqs, key
        assert action.shortcutContext() == Qt.ShortcutContext.WindowShortcut, key
        assert action in window.actions(), key
    # Ctrl+D is not the bookmark any more (the views duplicate with it)
    assert QKeySequence("Ctrl+D") not in window.action("bookmark_add").shortcuts()


def test_view_edit_keys_are_never_shortcuts(qtbot, window, data) -> None:
    open_ready(qtbot, window, data)
    forbidden = {QKeySequence(k).toString() for k in VIEW_KEYS}
    for action in window.findChildren(QAction):
        for seq in action.shortcuts():
            assert seq.toString() not in forbidden, (action.objectName(), seq.toString())
    for shortcut in window.findChildren(QShortcut):
        assert shortcut.key().toString() not in forbidden, shortcut.key().toString()
    # the menus show them for display only (the text after a tab)
    shown = {cmd.name: window.action(f"edit_{cmd.name}") for cmd in EDIT_COMMANDS}
    assert shown["edit_value"].text() == t("gui.actions.edit.value") + "\tEnter"
    assert shown["edit_json"].text().endswith("\tShift+Enter")
    assert shown["rename"].text().endswith("\tF2")
    assert shown["delete"].text().endswith("\tDel")
    assert shown["duplicate"].text().endswith("\tCtrl+D")
    assert shown["paste"].text().endswith("\tCtrl+V")
    assert shown["move_up"].text().endswith("\tAlt+Up") and shown["move_down"].text().endswith("\tAlt+Down")
    assert window.menus["edit_insert"].title().endswith("\tIns")
    for action in shown.values():
        assert action.shortcuts() == [] and action not in window.actions()
        assert action not in window.central.actions()


def test_line_edits_in_the_document_area_keep_their_keys(qtbot, window, data) -> None:
    """Enter, F2, Del, Ins, Ctrl+V, Ctrl+Z typed into a line edit inside a tab (find bars,
    the table's filter row, inline editors) stay with it."""
    tab = open_ready(qtbot, window, data)
    tab.edit.set(dirty=True, can_undo=True, steps=1, undo_text="Qiymat o'zgartirildi")
    activate(qtbot, window)
    tree = tab.active_view()
    assert isinstance(tree, EditTreeStub)
    line: QLineEdit = tree.edit
    returned = []
    line.returnPressed.connect(lambda: returned.append(True))
    line.setFocus()
    qtbot.waitUntil(line.hasFocus, timeout=TIMEOUT)
    qtbot.keyClicks(line, "abcd")
    qtbot.keyClick(line, Qt.Key.Key_Return)
    qtbot.keyClick(line, Qt.Key.Key_Enter)
    assert len(returned) == 2
    qtbot.keyClick(line, Qt.Key.Key_Home)
    qtbot.keyClick(line, Qt.Key.Key_Delete)
    assert line.text() == "bcd"
    QApplication.clipboard().setText("XY")
    qtbot.keyClick(line, Qt.Key.Key_V, CTRL)             # the line edit's own paste
    assert line.text() == "XYbcd"
    qtbot.keyClick(line, Qt.Key.Key_Z, CTRL)             # ... and its own undo
    assert line.text() == "bcd"
    none, alt = Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.AltModifier
    for key, mods in ((Qt.Key.Key_F2, none), (Qt.Key.Key_Insert, none), (Qt.Key.Key_D, CTRL),
                      (Qt.Key.Key_Up, alt), (Qt.Key.Key_Down, alt)):
        qtbot.keyClick(line, key, mods)
    view_calls = [c[0] for c in tree.calls if c[0] not in ("activate", "zoom")]
    assert not set(view_calls) & {"edit_value", "rename_key", "insert_node", "delete_selection",
                                  "duplicate_selection", "paste_json", "move_selection"}, view_calls
    assert "undo" not in tab.edit.names()
    # outside a line edit the window's Ctrl+Z / Ctrl+Y act on the document
    window.tabs.tabBar().setFocus()
    qtbot.waitUntil(lambda: window.tabs.tabBar().hasFocus(), timeout=TIMEOUT)
    qtbot.keyClick(window.tabs.tabBar(), Qt.Key.Key_Z, CTRL)
    assert tab.edit.names()[-1] == "undo"
    tab.edit.set(can_redo=True)
    qtbot.keyClick(window.tabs.tabBar(), Qt.Key.Key_Y, CTRL)
    qtbot.keyClick(window.tabs.tabBar(), Qt.Key.Key_Z, CTRL | SHIFT)
    assert tab.edit.names()[-2:] == ["redo", "redo"]
    qtbot.keyClick(window.tabs.tabBar(), Qt.Key.Key_S, CTRL)
    assert tab.edit.names()[-1] == "save"


def test_ctrl_b_adds_a_bookmark(qtbot, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    activate(qtbot, window)
    tab.session.select(NodeRef(5, ("name",)))
    asked = []
    window._ask_text = lambda title, label, default="": asked.append(default) or "beshinchi"
    window.tabs.tabBar().setFocus()
    qtbot.waitUntil(lambda: window.tabs.tabBar().hasFocus(), timeout=TIMEOUT)
    qtbot.keyClick(window.tabs.tabBar(), Qt.Key.Key_B, CTRL)
    assert asked == ["#5.name"]
    assert [b.name for b in tab.session.bookmarks] == ["beshinchi"]
    qtbot.keyClick(window.tabs.tabBar(), Qt.Key.Key_D, CTRL)           # no bookmark on Ctrl+D any more
    assert len(tab.session.bookmarks) == 1


# ----------------------------------------------------------------------------------------
# enabling and titles
# ----------------------------------------------------------------------------------------
def test_edit_actions_follow_the_controller(qtbot, window, data) -> None:
    a = window.action
    assert not a("save").isEnabled() and not a("undo").isEnabled()          # no tab
    tab = open_ready(qtbot, window, data)
    stub = tab.edit
    assert not a("save").isEnabled() and a("save_as").isEnabled()
    assert not a("undo").isEnabled() and not a("redo").isEnabled()
    assert a("bulk").isEnabled() and a("patch_import").isEnabled()
    assert not a("patch_export").isEnabled() and not a("discard").isEnabled()
    assert a("save").toolTip() == t("gui.main.save.nothing")
    stub.set(dirty=True, can_undo=True, steps=2, undo_text="Kalit qayta nomlandi")
    assert a("save").isEnabled() and a("undo").isEnabled() and not a("redo").isEnabled()
    assert a("patch_export").isEnabled() and a("discard").isEnabled()
    assert a("undo").text() == t("gui.main.act.undo_step", label="Kalit qayta nomlandi")
    assert a("redo").text() == t("gui.main.act.redo")
    stub.set(can_redo=True, redo_text="A & B")
    assert a("redo").text() == t("gui.main.act.redo_step", label="A && B")      # literal "&"
    stub.set(busy=True)
    for key in ("save", "save_as", "undo", "redo", "bulk", "patch_import", "patch_export", "discard",
                "edit_edit_value"):
        assert not a(key).isEnabled(), key
    assert a("undo").toolTip() == t("gui.actions.edit.busy")
    stub.set(busy=False, available=False, reason="locked")
    assert not a("undo").isEnabled() and not a("bulk").isEnabled() and not a("edit_delete").isEnabled()
    assert a("edit_delete").toolTip() == t("gui.actions.edit.reason.locked")
    assert not a("save").isEnabled() and a("save").toolTip() == t("gui.actions.edit.reason.locked")
    # the file grew: Save (offers Save As) and Save As stay; the changes can be exported / discarded
    stub.set(reason="file_grown")
    assert a("save").isEnabled() and a("save_as").isEnabled() and not a("undo").isEnabled()
    assert a("patch_export").isEnabled() and a("discard").isEnabled() and not a("patch_import").isEnabled()
    # the file changed: only the JSON Patch of the changes and Discard
    stub.set(reason="file_changed")
    assert not a("save").isEnabled() and not a("save_as").isEnabled()
    assert a("patch_export").isEnabled() and a("discard").isEnabled()
    assert a("save").toolTip() == t("gui.actions.edit.reason.file_changed")
    stub.set(available=True, reason="")
    for key, call in (("save", ("save",)), ("undo", ("undo",)), ("redo", ("redo",))):
        a(key).trigger()
        assert stub.calls[-1] == call
    a("discard").trigger()
    assert stub.calls[-1] == ("discard", window)


def test_titles_carry_the_dirty_mark(qtbot, window, data, tmp_path) -> None:
    tab = open_ready(qtbot, window, data)
    other = open_ready(qtbot, window, write_json(tmp_path / "b.json", records(3)))
    assert window.windowTitle() == "b.json — JSON Reshape"
    tab.edit.set(dirty=True, steps=1)
    assert window.tabs.tabText(window.tabs.indexOf(tab)) == f"{DIRTY_MARK}a.json"
    assert window.windowTitle() == "b.json — JSON Reshape"                 # the other tab is current
    window.tabs.setCurrentWidget(tab)
    assert window.windowTitle() == f"{DIRTY_MARK}a.json — JSON Reshape"
    assert tab.title() == "*a.json" and tab.display_name() == "*a.json"
    assert window.tabs.tabText(window.tabs.indexOf(other)) == "b.json"
    tab.edit.set(dirty=False)
    assert window.tabs.tabText(window.tabs.indexOf(tab)) == "a.json"
    assert window.windowTitle() == "a.json — JSON Reshape"


def test_view_edit_commands_of_the_edit_menu(qtbot, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    tree = tab.active_view()
    window._update_actions()
    for key, call in (("edit_edit_value", ("edit_value",)), ("edit_edit_json", ("edit_as_json",)),
                      ("edit_rename", ("rename_key",)), ("edit_insert_child", ("insert_node", "child")),
                      ("edit_insert_before", ("insert_node", "before")),
                      ("edit_insert_after", ("insert_node", "after")),
                      ("edit_duplicate", ("duplicate_selection",)), ("edit_delete", ("delete_selection",)),
                      ("edit_move_up", ("move_selection", -1)), ("edit_move_down", ("move_selection", 1)),
                      ("edit_type_number", ("change_type", "number")), ("edit_paste", ("paste_json",))):
        action = window.action(key)
        assert action.isEnabled(), key
        action.trigger()
        assert tree.calls[-1] == call, key
    # the view refines a command's state
    EditTreeStub.states = {"rename": (False, "Massiv elementining kaliti yo'q")}
    window._update_actions()
    assert not window.action("edit_rename").isEnabled()
    assert window.action("edit_rename").toolTip() == "Massiv elementining kaliti yo'q"
    # a view without the edit protocol: disabled, saying why
    tab.set_active_pane(1)
    assert isinstance(tab.active_view(), TextStub)
    assert not window.action("edit_edit_value").isEnabled()
    assert window.action("edit_edit_value").toolTip() == t("gui.actions.edit.not_here")
    assert not window.menus["edit_insert"].isEnabled() and not window.menus["edit_type"].isEnabled()


# ----------------------------------------------------------------------------------------
# node menu
# ----------------------------------------------------------------------------------------
class _ProtocolView:
    view_id = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def edit_value(self) -> None:
        self.calls.append(("edit_value",))

    def insert_node(self, where: str) -> None:
        self.calls.append(("insert_node", where))

    def change_type(self, kind: str) -> None:
        self.calls.append(("change_type", kind))

    def delete_selection(self) -> None:
        self.calls.append(("delete_selection",))


def _fake_session(app, edit, kind="array", member=False):
    doc = types.SimpleNamespace(kind=kind, member_records=member)
    def absolute_path(ref):
        return ((ref.record,) if ref.record is not None else ()) + tuple(ref.path)

    return types.SimpleNamespace(edit=edit, doc=doc, app=app, path="x.json", record_path=None, format=None,
                                 absolute_path=absolute_path)


def _items(menu) -> dict:
    out = {}
    for action in menu.actions():
        if action.menu() is not None:
            out[action.menu().objectName()] = action
            out.update(_items(action.menu()))
        elif action.objectName():
            out[action.objectName()] = action
    return out


def test_node_menu_edit_group(qtbot, app) -> None:
    from PySide6.QtWidgets import QWidget

    parent = QWidget()
    qtbot.addWidget(parent)
    view = _ProtocolView()
    edit = StubEditController()
    session = _fake_session(app, edit)
    menu = node_menu(parent, session, NodeRef(3, ("tags", 1)), view=view,
                     node=types.SimpleNamespace(kind="string", size=4))
    items = _items(menu)
    for name in ("edit_edit_value", "edit_edit_json", "edit_rename", "edit_insert_child", "edit_insert_before",
                 "edit_insert_after", "edit_duplicate", "edit_delete", "edit_move_up", "edit_move_down", "edit_paste",
                 "edit_insert_menu", "edit_type_menu"):
        assert name in items, name
    assert items["edit_edit_value"].text() == with_keys(t("gui.actions.edit.value"), "Enter")
    assert items["edit_edit_value"].isEnabled()
    assert not items["edit_edit_json"].isEnabled()                     # the view has no edit_as_json
    assert items["edit_edit_json"].toolTip() == t("gui.actions.edit.not_here")
    assert not items["edit_rename"].isEnabled()                        # no method AND an element
    assert not items["edit_insert_child"].isEnabled()                  # a string has no children
    assert items["edit_insert_child"].toolTip() == t("gui.actions.edit.need_container")
    assert items["edit_insert_before"].isEnabled()
    assert items["edit_type_string"].isChecked() and not items["edit_type_string"].isEnabled()
    assert items["edit_type_string"].toolTip() == t("gui.actions.edit.type.same")
    items["edit_type_number"].trigger()
    items["edit_insert_after"].trigger()
    items["edit_delete"].trigger()
    items["edit_edit_value"].trigger()
    assert view.calls == [("change_type", "number"), ("insert_node", "after"), ("delete_selection",), ("edit_value",)]
    # no shortcut is registered by the edit items (keys are text only)
    for name, action in items.items():
        if name.startswith("edit_"):
            assert action.shortcuts() == [], name
    menu.deleteLater()

    # a key rename of an array element / a record of a value document: disabled with the reason
    class Renamer(_ProtocolView):
        def rename_key(self) -> None:
            self.calls.append(("rename_key",))

    menu = node_menu(parent, session, NodeRef(3, ("tags", 1)), view=Renamer())
    assert _items(menu)["edit_rename"].toolTip() == t("gui.actions.edit.no_key_element")
    menu = node_menu(parent, session, NodeRef(3, ()), view=Renamer())
    assert _items(menu)["edit_rename"].toolTip() == t("gui.actions.edit.no_key_record")
    keyed = _fake_session(app, edit, kind="keyed", member=True)
    menu = node_menu(parent, keyed, NodeRef(3, ()), view=Renamer())
    assert _items(menu)["edit_rename"].isEnabled()                    # keyed: the record key
    menu = node_menu(parent, session, NodeRef(None, ()), view=Renamer())
    assert not _items(menu)["edit_insert_before"].isEnabled()          # the root has no siblings

    # no controller / unavailable / busy
    for state, reason in (({"available": False, "reason": "partial"}, t("gui.actions.edit.reason.partial")),
                          ({"available": True, "busy": True}, t("gui.actions.edit.busy"))):
        busy_edit = StubEditController(**state)
        menu = node_menu(parent, _fake_session(app, busy_edit), NodeRef(1, ("a",)), view=view)
        assert not _items(menu)["edit_edit_value"].isEnabled()
        assert _items(menu)["edit_edit_value"].toolTip() == reason
    menu = node_menu(parent, _fake_session(app, None), NodeRef(1, ("a",)), view=view)
    assert _items(menu)["edit_delete"].toolTip() == t("gui.actions.edit.no_editing")
    # a view without the protocol gets no edit group (unless asked for)
    plain = types.SimpleNamespace(view_id="plain")
    assert "edit_edit_value" not in _items(node_menu(parent, session, NodeRef(1, ("a",)), view=plain))
    forced = _items(node_menu(parent, session, NodeRef(1, ("a",)), view=plain, edit=True))
    assert not forced["edit_edit_value"].isEnabled()
    reasons = {k.rsplit(".", 1)[1] for k in MESSAGES if k.startswith("gui.actions.edit.reason.")}
    assert set(actions.EDIT_REASONS) <= reasons


def test_edit_reason_texts(qtbot) -> None:
    for reason in actions.EDIT_REASONS:
        stub = StubEditController(available=False, reason=reason)
        assert actions.edit_reason_text(stub) == t(f"gui.actions.edit.reason.{reason}")
    assert actions.edit_reason_text(StubEditController(available=False, reason="new_one")) == \
        t("gui.actions.edit.reason.other", reason="new_one")
    assert actions.edit_reason_text(StubEditController(available=False, reason="locked",
                                                       reason_text="Boshqa oynada ochiq")) == "Boshqa oynada ochiq"
    assert actions.edit_reason_text(None) == t("gui.actions.edit.no_editing")
    for kind in TYPE_KINDS:
        assert t(f"gui.actions.edit.type.{kind}") != f"gui.actions.edit.type.{kind}"


# ----------------------------------------------------------------------------------------
# close flows
# ----------------------------------------------------------------------------------------
def test_close_tab_asks_when_dirty(qtbot, window, data, tmp_path) -> None:
    tab = open_ready(qtbot, window, data)
    stub = tab.edit
    assert window.close_tab() is True                          # clean: closed at once, nothing asked
    assert "ask_close" not in stub.names() and window.tabs.count() == 0
    tab = open_ready(qtbot, window, data)
    stub = tab.edit
    stub.set(dirty=True, steps=1)
    stub.answer = "cancel"
    assert window.close_tab() is False
    assert stub.calls[-2:] == [("ask_close", "close"), ("answered", "cancel")]
    assert window.tabs.count() == 1 and not tab.closed
    stub.answer = "discard"
    assert window.close_tab() is True
    assert tab.closed and window.tabs.count() == 0 and stub.detached
    # an answer that comes later: the tab closes then (the ✕ button path, by index)
    tab = open_ready(qtbot, window, data)
    stub = tab.edit
    stub.set(dirty=True, steps=1)
    stub.answer = None
    window.tabs.tabCloseRequested.emit(0)
    assert window.tabs.count() == 1 and len(stub.questions) == 1
    assert window.close_tab(0) is False and len(stub.questions) == 1      # asked once only
    stub.answer_question("save")
    assert tab.closed and window.tabs.count() == 0


def test_window_close_asks_tab_by_tab(qtbot, app, window, data, tmp_path) -> None:
    first = open_ready(qtbot, window, data)
    clean = open_ready(qtbot, window, write_json(tmp_path / "c.json", records(2)))
    second = open_ready(qtbot, window, write_json(tmp_path / "b.json", records(3)))
    for tab in (first, second):
        tab.edit.set(dirty=True, steps=2)
    first.edit.answer = "discard"
    second.edit.answer = "cancel"
    window.close()
    assert not window._closing
    assert [tab.closed for tab in (first, clean, second)] == [False, False, False]
    assert ("ask_close", "quit") in first.edit.calls and ("ask_close", "quit") in second.edit.calls
    assert "ask_close" not in clean.edit.names()
    assert not first.dirty and second.dirty                    # the first one was discarded
    # again: only the dirty one asks; answered later -> the window closes afterwards
    first.edit.calls.clear()
    second.edit.answer = None
    window.close()
    assert not window._closing and len(second.edit.questions) == 1 and "ask_close" not in first.edit.names()
    window.close()                                             # a second close while asking: ignored
    assert len(second.edit.questions) == 1
    second.edit.answer_question("save")
    qtbot.waitUntil(lambda: window._closing, timeout=TIMEOUT)
    assert all(tab.closed for tab in (first, clean, second))


def test_window_close_waits_for_each_answer_in_turn(qtbot, window, data, tmp_path) -> None:
    a = open_ready(qtbot, window, data)
    b = open_ready(qtbot, window, write_json(tmp_path / "b.json", records(3)))
    for tab in (a, b):
        tab.edit.set(dirty=True, steps=1)
        tab.edit.answer = None
    window.close()
    assert len(a.edit.questions) == 1 and not b.edit.questions        # one question at a time
    assert window.current_tab() is a                                  # the tab asking is shown
    a.edit.answer_question("discard")
    assert len(b.edit.questions) == 1 and window.current_tab() is b
    b.edit.answer_question("cancel")
    qtbot.wait(50)
    assert not window._closing and not a.closed and not b.closed and b.dirty


def test_cancelled_window_close_reopens_the_tabs_saved_meanwhile(qtbot, window, data, tmp_path) -> None:
    a = open_ready(qtbot, window, data)
    b = open_ready(qtbot, window, write_json(tmp_path / "b.json", records(3)))
    for tab in (a, b):
        tab.edit.set(dirty=True, steps=1)
    a.edit.answer = "save"
    b.edit.answer = "cancel"
    saved = a.edit
    reopened = []
    real = a.session.reopen
    a.session.reopen = lambda **kw: (reopened.append(kw), real(**kw))
    window.close()
    assert not window._closing
    assert reopened == [{"discard_edits": True, "keep_view": True}]         # shows the saved file again
    wait_ready(qtbot, a.session)
    qtbot.waitUntil(lambda: a.edit is not None and a.edit is not saved, timeout=TIMEOUT)
    assert saved.detached and not b.closed and b.dirty


def test_headless_close_keeps_the_layers(qtbot, app, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    stub = tab.edit
    stub.set(dirty=True, steps=4)
    assert stub.answer == "keep"                    # what the controller does headless without a handler
    window.close()
    assert window._closing and tab.closed
    assert ("answered", "keep") in stub.calls and stub.detached


def test_close_without_questions(qtbot, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    stub = tab.edit
    stub.set(dirty=True, steps=1)
    stub.answer = None
    window.close_without_questions()
    assert window._closing and tab.closed and "ask_close" not in stub.names() and stub.detached


def test_close_all_tabs_stops_at_cancel(qtbot, window, data, tmp_path) -> None:
    a = open_ready(qtbot, window, data)
    b = open_ready(qtbot, window, write_json(tmp_path / "b.json", records(3)))
    b.edit.set(dirty=True, steps=1)
    b.edit.answer = "cancel"
    assert window.close_all_tabs() is False
    assert window.tabs.count() == 2
    b.edit.answer = "discard"
    results = []
    assert window.close_all_tabs(results.append) is True
    assert results == [True] and window.tabs.count() == 0 and a.closed and b.closed


def test_reopen_asks_first(qtbot, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    stub = tab.edit
    stub.set(dirty=True, steps=1)
    reopened = []
    real = tab.session.reopen
    tab.session.reopen = lambda **kw: (reopened.append(kw), real(**kw))
    stub.answer = "cancel"
    window.action("reopen").trigger()                  # F5
    assert reopened == [] and stub.calls[-2] == ("ask_close", "reopen")
    stub.answer = "keep"
    window.action("reopen").trigger()
    assert reopened == [{"discard_edits": True}]
    wait_ready(qtbot, tab.session)
    qtbot.waitUntil(lambda: tab.edit is not None and tab.edit is not stub, timeout=TIMEOUT)
    assert stub.detached


# ----------------------------------------------------------------------------------------
# Save As, bulk, JSON Patch
# ----------------------------------------------------------------------------------------
def test_save_as_keeps_the_format_or_offers_export(qtbot, window, data, tmp_path) -> None:
    tab = open_ready(qtbot, window, data)
    stub = tab.edit
    asked = []

    def ask_save(title, default, filters):
        asked.append((title, default, filters))
        return str(tmp_path / "copy.json")

    window._ask_save_path = ask_save
    window.action("save_as").trigger()
    assert stub.calls[-1] == ("save_as", str(tmp_path / "copy.json"))
    title, default, filters = asked[0]
    assert default == tab.session.path and "*.json" in filters and "*.jsonl" not in filters
    # another format's extension: Save As would not change the format -> the export is offered
    questions = []
    window._ask_yes_no = lambda title, text: questions.append(text) or True
    StubDialog.made.clear()
    assert window.save_as_current(tmp_path / "copy.csv") is False
    assert questions and ".csv" in questions[0]
    assert stub.calls[-1] == ("save_as", str(tmp_path / "copy.json"))      # not saved
    assert StubDialog.made and window.dialogs.get("export") is StubDialog.made[-1]
    window._ask_yes_no = lambda title, text: False
    assert window.save_as_current(tmp_path / "copy.jsonl") is False
    # compressed copies keep the format
    assert window.save_as_current(tmp_path / "copy.json.gz") is True
    assert stub.calls[-1] == ("save_as", str(tmp_path / "copy.json.gz"))
    window._ask_save_path = lambda *a: None                                  # cancelled
    calls = len(stub.calls)
    assert window.save_as_current() is False and len(stub.calls) == calls


def test_save_as_formats_by_kind() -> None:
    assert save_as_formats("array") == ("json",) and save_as_formats("keyed") == ("json",)
    assert save_as_formats("jsonl") == ("jsonl",) and set(save_as_formats("stream")) == {"json", "jsonl"}
    assert target_format("x.JSON") == "json" and target_format("x.ndjson.gz") == "jsonl"
    assert target_format("x.csv") == "csv" and target_format("x.geojson") is None and target_format("x") is None


def test_save_as_of_a_jsonl_gz_file_offers_its_patterns(qtbot, window, tmp_path) -> None:
    import gzip

    path = tmp_path / "lines.jsonl.gz"
    path.write_bytes(gzip.compress(b'{"a": 1}\n{"a": 2}\n'))
    tab = open_ready(qtbot, window, path)
    seen = []
    window._ask_save_path = lambda title, default, filters: seen.append(filters) or None
    window.action("save_as").trigger()
    assert "*.jsonl.gz" in seen[0] and "*.ndjson.gz" in seen[0] and "*.json.gz" not in seen[0]
    asked = []
    window._ask_yes_no = lambda title, text: asked.append(text) or False
    assert window.save_as_current(tmp_path / "out.json") is False            # .json for JSON Lines: asks
    assert asked and "save_as" not in tab.edit.names()
    assert window.save_as_current(tmp_path / "out.ndjson") is True
    assert tab.edit.calls[-1] == ("save_as", str(tmp_path / "out.ndjson"))


def test_bulk_dialog_one_per_document(qtbot, window, data, tmp_path) -> None:
    tab = open_ready(qtbot, window, data)
    dialog = window.show_bulk()
    assert isinstance(dialog, StubBulkDialog) and dialog.session is tab.session
    assert dialog.scope_default == "all" and dialog.parent() is window
    assert window.show_bulk() is dialog and len(StubBulkDialog.made) == 1
    other = open_ready(qtbot, window, write_json(tmp_path / "b.json", records(3)))
    second = window.show_bulk()
    assert second is not dialog and second.session is other.session
    window.close_tab(window.tabs.indexOf(other))             # its bulk dialog goes with it
    qtbot.waitUntil(lambda: not isValid(second) or not second.isVisible(), timeout=TIMEOUT)
    window.tabs.setCurrentWidget(tab)
    assert window.show_bulk() is dialog


def test_patch_import_export_and_discard(qtbot, app, window, data, tmp_path) -> None:
    tab = open_ready(qtbot, window, data)
    stub = tab.edit
    window._ask_open_path = lambda title, folder, filters: str(tmp_path / "in.patch.json")
    assert window.action("patch_import").isEnabled()
    window.action("patch_import").trigger()
    assert stub.calls[-1] == ("import_patch", str(tmp_path / "in.patch.json"))
    stub.set(dirty=True, steps=1, can_undo=True)
    defaults = []
    window._ask_save_path = lambda title, default, filters: defaults.append(default) or str(tmp_path / "o.json")
    window.action("patch_export").trigger()
    assert stub.calls[-1] == ("export_patch", str(tmp_path / "o.json"), True)
    assert defaults == [str(tmp_path / "a.patch.json")]
    assert app.state.get("last_patch_dir") == str(tmp_path)
    window.action("discard").trigger()
    assert stub.calls[-1] == ("discard", window)
    window._ask_open_path = lambda *a: None
    n = len(stub.calls)
    assert window.import_patch() is False and len(stub.calls) == n


def test_without_a_controller_the_commands_say_so(qtbot, app, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    tab.session.edit = None
    tab.session.edit_changed.emit()
    window._update_actions()
    for key in ("save", "save_as", "undo", "bulk", "patch_import", "edit_edit_value"):
        assert not window.action(key).isEnabled(), key
    assert window.action("edit_edit_value").toolTip() == t("gui.actions.edit.no_editing")
    assert window.save_current() is False
    assert window.statusBar().currentMessage() == t("gui.doctab.edit_unavailable")
