"""MainWindow (jsonreshape/gui/mainwindow.py; ARCHITECTURE §11.8, AGENT_PROMPT §4.6, §5, §11):
opening (open_path, the open dialog, drag & drop, open_requested with options), tabs
(titles, focus of an already open file, closing → sessions closed), menu actions routed to
the active pane's view, Ctrl+1..5, panes, theme, docks and dialogs (stubs), navigation
(back/forward, bookmarks, go to offset), recent files (missing ones removed), enabling by
context, window state saved/restored, and a smoke test with the real views."""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from guishell_helpers import (
    TIMEOUT,
    RawStub,
    StubDialog,
    StubFilterPanel,
    StubSearchPanel,
    TableStub,
    TextStub,
    TreeStub,
    make_app,
    records,
    stub_panels,
    stub_registry,
    wait_ready,
    wrapped_file,
    write_json,
)
from jsonreshape import __version__
from jsonreshape.errors import UserInputError
from jsonreshape.gui import theme
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.doctab import BUSY_MARK, DocumentTab
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.mainwindow import MainWindow, default_panel_factories, factory_available, parse_offset
from jsonreshape.gui.session import FilterSpec, NodeRef, same_node

pytestmark = pytest.mark.gui


@pytest.fixture
def app(qapp, tmp_path, lang_uz):
    ctx = make_app(tmp_path)
    yield ctx
    ctx.tasks.cancel_all()
    assert ctx.tasks.shutdown(30)


def new_window(qtbot, app, **kw) -> MainWindow:
    kw.setdefault("view_registry", stub_registry())
    kw.setdefault("panel_factories", stub_panels())
    w = MainWindow(app, **kw)
    qtbot.addWidget(w)
    return w


@pytest.fixture
def window(qtbot, app):
    StubDialog.made.clear()
    w = new_window(qtbot, app)
    yield w
    w.close()


@pytest.fixture
def data(tmp_path):
    return write_json(tmp_path / "a.json", records(50))


def open_ready(qtbot, window: MainWindow, path, options=None) -> DocumentTab:
    tab = window.open_path(path, options)
    wait_ready(qtbot, tab.session)
    assert tab.session.state == "ready", tab.session.open_error
    return tab


# ----------------------------------------------------------------------------------------
# opening and tabs
# ----------------------------------------------------------------------------------------
def test_empty_window(window) -> None:
    assert window.windowTitle() == "JSON Reshape"
    assert window.central.currentWidget() is window.welcome
    assert window.current_tab() is None and window.active_view() is None
    for key in ("copy_value", "copy_path", "export", "goto", "back", "forward", "bookmark_add", "filter", "sort",
                "clear_view", "close_tab", "reopen", "view_tree", "expand_all", "quick_find", "search"):
        assert not window.action(key).isEnabled(), key
    assert window.action("open").isEnabled() and window.action("settings").isEnabled()


def test_open_path_creates_a_tab(qtbot, window, data) -> None:
    opened = []
    window.tab_opened.connect(opened.append)
    tab = window.open_path(data)
    assert opened == [tab] and window.current_tab() is tab
    assert window.tabs.tabText(0).startswith(BUSY_MARK) or tab.session.state == "ready"
    wait_ready(qtbot, tab.session)
    qtbot.waitUntil(lambda: window.tabs.tabText(0) == "a.json", timeout=TIMEOUT)
    assert window.tabs.tabToolTip(0) == str(data)
    assert window.windowTitle() == "a.json — JSON Reshape"
    assert window.central.currentWidget() is window.tabs
    assert window.status.count_label.full_text() == "50 yozuv"
    assert window.state.recent_files()[0] == str(data)


def test_same_file_twice_and_open_dialog_focuses(qtbot, window, data, tmp_path) -> None:
    first = open_ready(qtbot, window, data)
    second = open_ready(qtbot, window, data)                  # a second view of the file is allowed
    assert window.tabs.count() == 2 and first.session is not second.session
    window.tabs.setCurrentWidget(first)
    other = write_json(tmp_path / "b.json", records(3))
    window._ask_open_files = lambda: [str(data), str(other)]
    tabs = window.open_dialog()
    assert tabs[0] in (first, second)                         # focused, not opened again
    assert window.tabs.count() == 3
    assert window.state.get("last_dir") == str(tmp_path)
    window._ask_open_files = lambda: []
    assert window.open_dialog() == [] and window.tabs.count() == 3


def test_drag_and_drop_opens_files(qtbot, window, tmp_path) -> None:
    a = write_json(tmp_path / "x.json", records(2))
    b = tmp_path / "y.jsonl"
    b.write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(a)), QUrl.fromLocalFile(str(b))])
    enter = QDragEnterEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
    window.dragEnterEvent(enter)
    assert enter.isAccepted()
    drop = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier)
    window.dropEvent(drop)
    assert [t.session.name for t in window.tab_widgets()] == ["x.json", "y.jsonl"]
    for tab in window.tab_widgets():
        wait_ready(qtbot, tab.session)
    assert window.tab_widgets()[1].session.doc.kind == "jsonl"
    text = QMimeData()
    text.setText("not a file")
    enter = QDragEnterEvent(QPointF(1, 1), Qt.DropAction.CopyAction, text, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
    window.dragEnterEvent(enter)
    assert not enter.isAccepted()
    assert window.acceptDrops()


def test_open_requested_with_options(qtbot, app, window, tmp_path) -> None:
    path = wrapped_file(tmp_path / "w.json", 20)
    app.request_open(str(path), {"record_path": "other"})
    tab = window.current_tab()
    wait_ready(qtbot, tab.session)
    assert tab.session.record_path == "other" and tab.session.doc.record_count == 3
    ref = NodeRef(4, ("contact", "zip"))
    app.request_open(str(path), {"ref": ref})
    tab2 = window.current_tab()
    assert tab2 is not tab and window.tabs.count() == 2
    qtbot.waitUntil(lambda: same_node(tab2.session.current, ref), timeout=TIMEOUT)


def test_close_tab_closes_the_session(qtbot, app, window, data, tmp_path) -> None:
    a = open_ready(qtbot, window, data)
    b = open_ready(qtbot, window, write_json(tmp_path / "b.json", records(5)))
    tree_b = b.panes[0].current_view()
    search = window.panels["search"]
    assert search.session is b.session
    closed = []
    window.tab_closed.connect(closed.append)
    window.close_tab()                                        # the current one: b
    assert closed == [b] and b.session.state == "closed" and b.closed
    assert ("shutdown",) in tree_b.calls
    assert window.current_tab() is a and search.session is a.session
    window.action("close_tab").trigger()
    assert window.tabs.count() == 0 and a.session.state == "closed"
    assert search.session is None and window.panels["filter"].session is None
    assert window.windowTitle() == "JSON Reshape" and window.central.currentWidget() is window.welcome
    assert app.tasks.wait_idle(30) and not app.errors


def test_close_while_indexing(qtbot, app, window, tmp_path) -> None:
    path = tmp_path / "big.json"
    path.write_bytes(b"[" + b",".join(b'{"i":%d}' % i for i in range(200_000)) + b"]")
    tab = window.open_path(path)
    window.close_tab(0)
    assert tab.session.state == "closed"
    assert app.tasks.wait_idle(30) and not app.errors


# ----------------------------------------------------------------------------------------
# actions routed to the active view
# ----------------------------------------------------------------------------------------
def test_view_actions_go_to_the_active_view(qtbot, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    tree = tab.active_view()
    assert isinstance(tree, TreeStub)
    for key in ("copy_value", "copy_path", "copy_more_value_python", "copy_more_path_pointer", "copy_more_key",
                "zoom_in", "zoom_out", "zoom_reset", "quick_find", "find_next", "find_prev", "expand_all",
                "collapse_all", "expand_level_3"):
        assert window.action(key).isEnabled(), key
        window.action(key).trigger()
    window.action("sort_keys").setChecked(True)
    assert tree.calls[-15:] == [
        ("copy_value", "json"), ("copy_path", "dotted"), ("copy_value", "python"), ("copy_path", "pointer"),
        ("copy_key",), ("zoom", 1), ("zoom", -1), ("zoom", 0), ("quick_find",), ("find_next", False),
        ("find_next", True), ("expand_all_current",), ("collapse_all",), ("expand_to_level", 3),
        ("toggle_sort_keys", True)]
    # the other pane becomes active: text view, no tree commands
    tab.set_active_pane(1)
    text = tab.active_view()
    assert isinstance(text, TextStub)
    assert not window.action("expand_all").isEnabled() and not window.action("collapse_all").isEnabled()
    assert not window.menus["expand_level"].isEnabled()
    window.action("zoom_in").trigger()
    window.action("copy_value").trigger()
    assert text.calls[-2:] == [("zoom", 1), ("copy_value", "json")]


def test_view_kind_actions_and_shortcuts(qtbot, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    expected = {"tree": "Ctrl+1", "table": "Ctrl+2", "text": "Ctrl+3", "raw": "Ctrl+4", "schema": "Ctrl+5"}
    for kind, keys in expected.items():
        assert window.action(f"view_{kind}").shortcut() == QKeySequence(keys)
    assert window.action("view_tree").isChecked()
    window.action("view_raw").trigger()
    assert tab.active_kind() == "raw" and isinstance(tab.active_view(), RawStub)
    assert window.action("view_raw").isChecked()
    tab.set_active_pane(1)
    assert window.action("view_text").isChecked()
    window.action("view_schema").trigger()
    assert tab.panes[1].current_kind() == "schema" and tab.panes[0].current_kind() == "raw"


def test_keyboard_shortcut_switches_the_view(qtbot, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    window.show()
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window, timeout=TIMEOUT)
    tree = tab.active_view()
    tree.edit.setFocus()
    qtbot.waitUntil(lambda: tree.edit.hasFocus(), timeout=TIMEOUT)
    qtbot.keyClick(tree.edit, Qt.Key.Key_2, Qt.KeyboardModifier.ControlModifier)
    assert tab.active_kind() == "table"
    table = tab.active_view()
    table.edit.setFocus()
    qtbot.waitUntil(lambda: table.edit.hasFocus(), timeout=TIMEOUT)
    qtbot.keyClick(table.edit, Qt.Key.Key_F3)
    assert table.calls[-1] == ("find_next", False)
    qtbot.keyClick(table.edit, Qt.Key.Key_F3, Qt.KeyboardModifier.ShiftModifier)
    assert table.calls[-1] == ("find_next", True)


def test_shortcuts_of_the_spec(window) -> None:
    # stage 3 (ARCHITECTURE §12.9): the bookmark moved from Ctrl+D to Ctrl+B -- Ctrl+D duplicates
    # nodes in the views (AGENT_PROMPT §8.1); Save / Undo / Redo / Bulk are window shortcuts
    expected = {"open": "Ctrl+O", "close_tab": "Ctrl+W", "copy_value": "Ctrl+C", "copy_path": "Ctrl+Shift+C",
                "quick_find": "Ctrl+F", "find_next": "F3", "find_prev": "Shift+F3", "search": "Ctrl+Shift+F",
                "filter": "Ctrl+Shift+L", "goto": "Ctrl+G", "back": "Alt+Left", "forward": "Alt+Right",
                "export": "Ctrl+E", "reopen": "F5", "bookmark_add": "Ctrl+B", "bookmarks": "Ctrl+Shift+D",
                "zoom_reset": "Ctrl+0", "expand_all": "*", "save": "Ctrl+S", "save_as": "Ctrl+Shift+S",
                "undo": "Ctrl+Z", "redo": "Ctrl+Y", "bulk": "Ctrl+H"}
    for key, seq in expected.items():
        assert QKeySequence(seq) in window.action(key).shortcuts(), (key, window.action(key).shortcuts())
    assert QKeySequence("Ctrl+=") in window.action("zoom_in").shortcuts()
    assert QKeySequence("Ctrl+-") in window.action("zoom_out").shortcuts()
    # view commands are bound to the document area so the docks keep their own keys
    for key in ("copy_value", "copy_path", "zoom_in", "quick_find", "find_next", "expand_all"):
        action = window.action(key)
        assert action.shortcutContext() == Qt.ShortcutContext.WidgetWithChildrenShortcut
        assert action in window.central.actions()


def test_no_ambiguous_shortcuts(window) -> None:
    """Two active actions on one key cancel each other in Qt: every key is used once."""
    seen: dict[str, str] = {}
    for key, action in window.acts.items():
        for seq in action.shortcuts():
            text = seq.toString()
            assert text not in seen, (text, seen.get(text), key)
            seen[text] = key
    assert window.action("close_tab").shortcuts() == [QKeySequence("Ctrl+W"), QKeySequence("Ctrl+F4")]
    assert [s.toString() for s in window.action("zoom_out").shortcuts()].count("Ctrl+-") == 1


def test_panes_actions(qtbot, app, window, data, tmp_path) -> None:
    tab = open_ready(qtbot, window, data)
    assert window.action("second_pane").isChecked()
    window.action("second_pane").trigger()
    assert not tab.second_pane_visible() and app.state.get("second_pane") is False
    window.action("split_vertical").trigger()
    assert tab.orientation() == Qt.Orientation.Vertical
    tab2 = open_ready(qtbot, window, write_json(tmp_path / "b.json", records(2)))
    assert not tab2.second_pane_visible() and tab2.orientation() == Qt.Orientation.Vertical
    assert not window.action("second_pane").isChecked() and window.action("split_vertical").isChecked()
    window.action("second_pane").trigger()
    assert tab2.second_pane_visible()
    window.tabs.setCurrentWidget(tab)
    assert not window.action("second_pane").isChecked()      # follows the tab


def test_theme_switching(qtbot, qapp, app, window, tmp_path) -> None:
    try:
        assert window.action("theme_system").isChecked()
        window.action("theme_dark").trigger()
        assert theme.mode() == "dark" and theme.current_scheme() == "dark"
        assert app.state.get("theme") == "dark"
        # saved by a silent background task (review finding T9: no file lock on the GUI thread)
        qtbot.waitUntil(lambda: not app.state_saving, timeout=TIMEOUT)
        assert GuiState.load(tmp_path / "gui_state.json").get("theme") == "dark"
        assert window.action("theme_dark").isChecked() and not window.action("theme_system").isChecked()
        window.action("theme_light").trigger()
        assert theme.mode() == "light"
    finally:
        theme.apply_theme(qapp, "system")
        theme._reset_for_tests()


# ----------------------------------------------------------------------------------------
# docks and dialogs
# ----------------------------------------------------------------------------------------
def test_search_and_filter_docks(qtbot, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    search: StubSearchPanel = window.panels["search"]
    filt: StubFilterPanel = window.panels["filter"]
    assert search.session is tab.session and filt.session is tab.session
    assert window.docks["search"].objectName() == "dock_search"
    assert not window.docks["search"].isVisibleTo(window)
    window.action("search").trigger()
    assert window.docks["search"].isVisibleTo(window) and search.calls[-1] == ("focus_search", None)
    window.action("filter").trigger()
    assert window.docks["filter"].isVisibleTo(window) and filt.calls[-1] == ("focus_expression",)
    schema = tab.set_active_view_kind("schema")
    schema.filter_field_requested.emit("contact.zip")
    assert filt.calls[-1] == ("add_condition", "contact.zip", "exists", None)
    window.status.tasks_clicked.emit()
    assert window.docks["tasks"].isVisibleTo(window)


def test_search_takes_the_selected_text(qtbot, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    window.show()
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window, timeout=TIMEOUT)
    tree = tab.active_view()
    tree.edit.setText("street 12 and more")
    tree.edit.setFocus()
    qtbot.waitUntil(lambda: tree.edit.hasFocus(), timeout=TIMEOUT)
    tree.edit.setSelection(0, 9)
    assert window.selected_text() == "street 12"
    window.action("search").trigger()
    search = window.panels["search"]
    assert search.calls[-1] == ("focus_search", "street 12")
    assert window.docks["search"].isVisible()                 # shown before the focus moves
    tree.selected_text = lambda: "from the view"
    window.action("search").trigger()
    assert search.calls[-1] == ("focus_search", "from the view")


def test_closing_shuts_the_panels_down(qtbot, app, data) -> None:
    w = new_window(qtbot, app)
    tab = open_ready(qtbot, w, data)
    search, filt = w.panels["search"], w.panels["filter"]
    w.close()
    assert search.calls[-2:] == [("set_session", None), ("shutdown",)]
    assert filt.calls[-2:] == [("set_session", None), ("shutdown",)]
    assert tab.session.state == "closed"


def test_sort_export_settings_dialogs(qtbot, app, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    window.action("sort").trigger()
    sort = StubDialog.made[-1]
    assert sort.args == (tab.session, window) and sort.isVisible()
    window.action("export").trigger()
    export = StubDialog.made[-1]
    # "selected records": the bulk dialog's selection source (asked when needed; stage 5)
    selection = export.kwargs.pop("selection")
    assert callable(selection) and selection() is None              # the stub views select nothing
    assert export.args == (tab.session, window) and export.kwargs == {"columns": None, "scope": "view"}
    tab.set_active_view_kind("table")
    window.action("export").trigger()
    kwargs = dict(StubDialog.made[-1].kwargs)
    assert callable(kwargs.pop("selection")) and kwargs == {"columns": TableStub.columns, "scope": "view"}
    window.action("settings").trigger()
    assert StubDialog.made[-1].args == (app, window)
    for dialog in list(StubDialog.made):
        dialog.close()


def test_missing_panels_disable_their_commands(qtbot, app, data) -> None:
    def broken(*_args, **_kw):
        raise RuntimeError("no panel")

    w = new_window(qtbot, app, panel_factories={"search": broken, "filter": None, "sort": None, "export": broken,
                                                "settings": None})
    try:
        assert "search" not in w.panels and "filter" not in w.panels
        tab = open_ready(qtbot, w, data)
        assert not w.action("search").isEnabled() and not w.action("filter").isEnabled()
        assert not w.action("sort").isEnabled() and not w.action("settings").isEnabled()
        assert w.action("export").isEnabled()                 # its factory exists but fails when used
        w.action("export").trigger()
        assert any(isinstance(e, RuntimeError) for e in app.errors)
        assert tab.session.state == "ready"
    finally:
        w.close()


def test_default_panel_factories_are_lazy() -> None:
    factories = default_panel_factories()
    # stage 3 (ARCHITECTURE §12.9) adds the bulk operations dialog (Ctrl+H), stage 5 the statistics dock
    assert set(factories) == {"search", "filter", "stats", "sort", "export", "settings", "bulk"}
    assert factory_available(None) is False
    assert factory_available(lambda: None) is True
    for factory in factories.values():
        assert isinstance(factory_available(factory), bool)


def test_single_document_disables_filter_and_sort(qtbot, window, tmp_path) -> None:
    open_ready(qtbot, window, write_json(tmp_path / "s.json", {"a": [1, 2], "b": {"c": 1}}))
    assert not window.action("filter").isEnabled() and not window.action("sort").isEnabled()
    assert not window.action("clear_view").isEnabled()
    assert window.action("search").isEnabled() and window.action("export").isEnabled()


def test_clear_filter_and_sort(qtbot, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    s = tab.session
    assert not window.action("clear_view").isEnabled()
    s.set_filter(FilterSpec("id < 10"))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 10, timeout=TIMEOUT)
    assert window.action("clear_view").isEnabled()
    window.action("clear_view").trigger()
    assert s.view_is_base and s.filter is None
    assert not window.action("clear_view").isEnabled()


# ----------------------------------------------------------------------------------------
# navigation
# ----------------------------------------------------------------------------------------
def test_back_forward(qtbot, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    s = tab.session
    assert not window.action("back").isEnabled()
    s.select(NodeRef(1, ()))
    s.select(NodeRef(2, ("name",)))
    assert window.action("back").isEnabled() and not window.action("forward").isEnabled()
    window.action("back").trigger()
    assert same_node(s.current, NodeRef(1, ()))
    assert window.action("forward").isEnabled()
    window.action("forward").trigger()
    assert same_node(s.current, NodeRef(2, ("name",)))


def test_add_bookmark_and_bookmarks_dialog(qtbot, app, window, data, tmp_path) -> None:
    tab = open_ready(qtbot, window, data)
    asked = []

    def ask(title, label, default):
        asked.append(default)
        return "Muhim joy"

    window._ask_text = ask
    window.action("bookmark_add").trigger()                  # no current node: a message only
    assert asked == [] and window.status.currentMessage() == "Avval tugunni tanlang."
    tab.session.select(NodeRef(7, ("contact", "address")))
    window.action("bookmark_add").trigger()
    assert asked == ["#7.contact.address"]
    assert [b.name for b in tab.session.bookmarks] == ["Muhim joy"]
    qtbot.waitUntil(lambda: not app.state_saving, timeout=TIMEOUT)     # saved in the background (T9)
    stored = GuiState.load(tmp_path / "gui_state.json").bookmarks(tab.session.path)
    assert [(b.name, b.record, b.path) for b in stored] == [("Muhim joy", 7, ["contact", "address"])]
    window._ask_text = lambda *a: None                        # cancelled
    window.action("bookmark_add").trigger()
    assert len(tab.session.bookmarks) == 1
    window.action("bookmarks").trigger()
    dialog = window.dialogs["bookmarks"]
    assert dialog.count() == 1 and dialog.isVisible()
    dialog.close()


def test_goto_dialog_from_the_menu(qtbot, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    window.action("goto").trigger()
    dialog = window.dialogs["goto"]
    dialog.set_text("#12.name")
    with qtbot.waitSignal(dialog.resolved, timeout=TIMEOUT):
        dialog.accept()
    assert same_node(tab.session.current, NodeRef(12, ("name",)))


def test_goto_offset(qtbot, app, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    window._ask_text = lambda *a: "0x10"
    window.action("goto_offset").trigger()
    raw = tab.active_view()
    assert isinstance(raw, RawStub) and raw.calls[-1] == ("goto_offset", 16, 1)
    window._ask_text = lambda *a: "1 000"
    window.action("goto_offset").trigger()
    assert raw.calls[-1] == ("goto_offset", 1000, 1)
    window._ask_text = lambda *a: "abc"
    window.action("goto_offset").trigger()
    assert isinstance(app.errors[-1], UserInputError) and app.errors[-1].key == "gui.main.offset.invalid"


def test_parse_offset() -> None:
    assert parse_offset("1048576") == 1048576
    assert parse_offset(" 1 048 576 ") == 1048576
    assert parse_offset("0x100000") == 0x100000
    assert parse_offset("1_000,5") == 10005
    for bad in ("", "-1", "x", "0x", "1.5"):
        with pytest.raises(UserInputError):
            parse_offset(bad)


def test_record_path_action(qtbot, window, tmp_path) -> None:
    tab = open_ready(qtbot, window, wrapped_file(tmp_path / "w.json", 10))
    window.action("record_path").trigger()
    dialog = window.dialogs["record_path"]
    assert dialog.session is tab.session and dialog.candidate_count() >= 2
    dialog.reject()


def test_reopen_action(qtbot, window, data) -> None:
    tab = open_ready(qtbot, window, data)
    write_json(data, records(60))
    with qtbot.waitSignal(tab.session.index_finished, timeout=TIMEOUT):
        window.action("reopen").trigger()
    assert tab.session.doc.record_count == 60


# ----------------------------------------------------------------------------------------
# recent files
# ----------------------------------------------------------------------------------------
def test_recent_files_menu(qtbot, app, window, data, tmp_path) -> None:
    other = write_json(tmp_path / "b.json", records(2))
    open_ready(qtbot, window, other)
    tab = open_ready(qtbot, window, data)
    actions = window.recent_actions()
    assert [a.data() for a in actions] == [str(data), str(other)]
    assert actions[0].text() == "&1 a.json"
    window.tabs.setCurrentIndex(0)
    actions[0].trigger()                                     # already open: focused
    assert window.current_tab() is tab and window.tabs.count() == 2
    # a file that disappeared is removed with a message
    missing = tmp_path / "gone.json"
    app.state.add_recent(missing)
    gone = window.recent_actions()[0]
    assert gone.data() == str(missing)
    gone.trigger()
    failed = window.current_tab()
    qtbot.waitUntil(lambda: failed.session.state == "failed", timeout=TIMEOUT)
    assert str(missing) not in app.state.recent_files()
    assert "gone.json" in window.status.currentMessage()
    assert failed.info_bar("open_failed") is not None
    # clear
    window._fill_recent_menu()
    clear = next(a for a in window.menus["recent"].actions() if a.objectName() == "recent_clear")
    clear.trigger()
    assert app.state.recent_files() == []
    window._fill_recent_menu()
    assert window.recent_actions() == []
    assert not window.menus["recent"].actions()[0].isEnabled()   # "Ro'yxat bo'sh"


# ----------------------------------------------------------------------------------------
# tools / help
# ----------------------------------------------------------------------------------------
def test_about_and_environment(qtbot, window) -> None:
    text = window.about_text()
    assert __version__ in text and "oflayn" in text and "Tezkor yadro" in text
    box = window.show_about()
    assert box.isVisible()
    box.close()
    window.action("environment").trigger()
    qtbot.waitUntil(lambda: "environment" in window.dialogs, timeout=TIMEOUT)
    dialog = window.dialogs["environment"]
    assert dialog.isVisible() and dialog.windowTitle() == "Muhit haqida"
    report = dialog.findChild(QPlainTextEdit).toPlainText()
    assert __version__ in report and "Python" in report
    dialog.close()


# ----------------------------------------------------------------------------------------
# window state
# ----------------------------------------------------------------------------------------
def test_window_state_saved_and_restored(qtbot, qapp, tmp_path, data) -> None:
    state_file = tmp_path / "gui_state.json"
    app1 = AppContext(state=GuiState.load(state_file), headless=True)
    w1 = MainWindow(app1, view_registry=stub_registry(), panel_factories=stub_panels())
    qtbot.addWidget(w1)
    w1.show()
    w1.resize(700, 500)                                       # fits the offscreen screen (800x600)
    w1.show_filter()
    qtbot.waitUntil(lambda: w1.docks["filter"].isVisible(), timeout=TIMEOUT)
    qtbot.wait(50)
    size0 = (w1.width(), w1.height())           # 700 x 500 unless the platform enforces a minimum
    tab = w1.open_path(data)
    wait_ready(qtbot, tab.session)
    qtbot.wait(50)
    size1 = (w1.width(), w1.height())
    assert size1 == size0                       # opening a file (status texts, tasks) did not widen it
    closed = []
    w1.closed.connect(lambda: closed.append(True))
    w1.close()
    assert closed and tab.session.state == "closed"
    saved = GuiState.load(state_file)
    assert saved.get("geometry") and saved.get("window_state")
    app2 = AppContext(state=GuiState.load(state_file), headless=True)
    w2 = MainWindow(app2, view_registry=stub_registry(), panel_factories=stub_panels())
    qtbot.addWidget(w2)
    w2.show()
    try:
        qtbot.waitUntil(lambda: w2.docks["filter"].isVisible(), timeout=TIMEOUT)
        assert not w2.docks["search"].isVisible()
        assert (w2.width(), w2.height()) == size1
    finally:
        w2.close()


# ----------------------------------------------------------------------------------------
# the real panels and dialogs (stub views)
# ----------------------------------------------------------------------------------------
def panel_session(panel):
    session = panel.session          # a method on SearchPanel, a property on FilterPanel
    return session() if callable(session) else session


def test_real_panels_and_dialogs(qtbot, app, data) -> None:
    for name in ("searchpanel", "filterpanel", "sortdialog", "exportdialog", "settingsdialog"):
        pytest.importorskip(f"jsonreshape.gui.{name}")
    w = MainWindow(app, view_registry=stub_registry())
    qtbot.addWidget(w)
    w.show()
    try:
        assert {"search", "filter", "tasks"} <= set(w.panels)
        tab = open_ready(qtbot, w, data)
        search, filt = w.panels["search"], w.panels["filter"]
        assert panel_session(search) is tab.session and panel_session(filt) is tab.session
        w.action("filter").trigger()
        assert w.docks["filter"].isVisible()
        schema = tab.set_active_view_kind("schema")
        schema.filter_field_requested.emit("contact.zip")
        rows = filt.current_editor().root.rows()
        assert rows and rows[-1].field_text() == "contact.zip"
        w.action("search").trigger()
        assert w.docks["search"].isVisible()
        w.action("sort").trigger()
        sort = w.dialogs["sort"]
        assert sort.isVisible() and type(sort).__name__ == "SortDialog"
        sort.reject()
        tab.set_active_view_kind("table")
        w.action("export").trigger()
        export = w.dialogs["export"]
        assert type(export).__name__ == "ExportDialog" and export.columns == TableStub.columns
        export.reject()
        w.action("settings").trigger()
        settings = w.dialogs["settings"]
        assert type(settings).__name__ == "SettingsDialog"
        settings.reject()
        w.close_tab()
        assert panel_session(search) is None and panel_session(filt) is None
    finally:
        w.close()
    assert app.tasks.wait_idle(30)
    assert not app.errors, app.errors


# ----------------------------------------------------------------------------------------
# smoke test with the real views
# ----------------------------------------------------------------------------------------
def test_smoke_with_the_default_registry(qtbot, app, tmp_path) -> None:
    for name in ("treeview", "tableview", "textview", "rawview", "schemaview"):
        pytest.importorskip(f"jsonreshape.gui.{name}")
    w = MainWindow(app)
    qtbot.addWidget(w)
    w.show()
    try:
        path = wrapped_file(tmp_path / "w.json", 300)
        tab = open_ready(qtbot, w, path)
        assert tab.available_kinds() == ["tree", "table", "text", "raw", "schema", "mapping"]
        for pane in (0, 1):
            tab.set_active_pane(pane)
            for kind in ("tree", "table", "text", "raw", "schema", "mapping"):
                w.action(f"view_{kind}").trigger()
                assert tab.active_kind() == kind
                qtbot.wait(30)
        tab.session.select(NodeRef(5, ("contact", "address")), reveal=True)
        qtbot.wait(200)
        for key in ("zoom_in", "zoom_reset", "quick_find"):
            w.action(key).trigger()
        lines = tmp_path / "l.jsonl"
        lines.write_text("\n".join(json.dumps({"i": i}) for i in range(100)) + "\n", encoding="utf-8")
        tab2 = open_ready(qtbot, w, lines)
        w.action("view_table").trigger()
        qtbot.wait(200)
        w.close_tab(w.tabs.indexOf(tab2))
        assert app.tasks.wait_idle(30)
        assert not app.errors, app.errors
    finally:
        w.close()


def test_smoke_with_the_real_views_available_now(qtbot, app, tmp_path) -> None:
    """Whatever real views exist (the full set is covered above): switch through them in
    both panes, follow a selection, jump to a JSON error in the raw view."""
    from jsonreshape.gui.doctab import default_view_registry

    registry = default_view_registry()
    if not registry:
        pytest.skip("no real view module is available yet")
    w = MainWindow(app, view_registry=registry)
    qtbot.addWidget(w)
    w.show()
    try:
        tab = open_ready(qtbot, w, wrapped_file(tmp_path / "w.json", 300))
        for pane in (0, 1):
            tab.set_active_pane(pane)
            for kind in tab.available_kinds():
                w.action(f"view_{kind}").trigger()
                assert tab.active_kind() == kind
                qtbot.wait(20)
        tab.session.select(NodeRef(5, ("contact", "address")), reveal=True)
        qtbot.wait(200)
        bad = tmp_path / "bad.json"
        bad.write_text('[{"a": 1},\n {"a": 2},\n {"a": ]', encoding="utf-8")
        tab2 = open_ready(qtbot, w, bad)
        bar = tab2.info_bar("json_error")
        assert bar is not None
        if "raw" in registry:
            raw = tab2.go_to_error()
            assert raw is not None and tab2.panes[1].current_kind() == "raw"
            qtbot.wait(200)
        for t_ in w.tab_widgets():
            w.close_tab(w.tabs.indexOf(t_))
        assert app.tasks.wait_idle(30)
        assert not app.errors, app.errors
    finally:
        w.close()
