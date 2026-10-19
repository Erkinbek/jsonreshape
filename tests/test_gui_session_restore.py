"""Session restore on real windows (jsonreshape/gui/workspace.py ``WorkspaceController`` + ``TabRestorer``,
gui/sessionstore.py, the hooks in mainwindow.py / app.py / settingsdialog.py, ``TreeView.expanded_locations`` /
``restore_expanded``; docs/ARCHITECTURE.md §15.0 "Session restore").

A window is closed (the session goes into the GUI state file) and a new one starts on the same file: the tabs
come back with their panes' views, current node, expanded tree nodes, filter / quick filters / sort (re-run in
the background) and search inputs -- for array, wrapped (with a forced record path), keyed, JSON Lines (with a
forced format) and single documents. Missing files are skipped with one status line, changed files reopen
normally (what no longer exists is skipped), a search result view is not restored (its search is), unsaved
changes come back only through the recovery bar. The ``restore_session`` setting (always / ask -- the
non-modal bar -- / never), files on the command line (``app.main``: only they open, the saved session is kept
for the next start), the injected autosave interval, a capture while the restore is still on its way, and the
Settings dialog's row."""

from __future__ import annotations

import gc
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from guishell_helpers import TIMEOUT, wait_ready, wrapped_file, write_json
from jsonreshape.gui import sessionstore as ss
from jsonreshape.gui import widgets
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.mainwindow import MainWindow
from jsonreshape.gui.session import FilterSpec, NodeRef
from jsonreshape.i18n import t
from jsonreshape.sortkeys import SortKey

pytestmark = pytest.mark.gui


def people(path: Path, n: int = 60) -> Path:
    """``n`` records; record keys in this order: id, name, contact (city, zip, tags), price (exact 1.10)."""
    recs = [{"id": i, "name": f"Oʻzbek {i}" if i % 2 else f"Тошкент {i}",
             "contact": {"city": "Toshkent" if i % 3 else "Xiva", "zip": 100000 + i, "tags": [i, "a", {"x": i}]},
             "price": "@P@"} for i in range(n)]
    text = json.dumps(recs, ensure_ascii=False, indent=1).replace('"@P@"', "1.10")
    path.write_text(text, encoding="utf-8")
    return path


class Shell:
    """Windows on ONE GUI state file, like successive starts of the program."""

    def __init__(self, qtbot: Any, tmp_path: Path) -> None:
        self.qtbot = qtbot
        self.state_path = tmp_path / "gui_state.json"
        self.win: MainWindow | None = None
        self.app: AppContext | None = None
        self.started: list[MainWindow] = []

    def start(self, *, command_line: tuple = (), mode: str | None = None, interval: int | None = None,
              decide: bool = True) -> MainWindow:
        state = GuiState.load(self.state_path)
        if mode is not None:
            state.set(ss.RESTORE_KEY, mode)
        app = AppContext(state=state, headless=True)
        app.tasks.set_poll_interval(10)
        app.errors = []
        app.statuses = []
        app.error_reported.connect(app.errors.append)
        app.status_message.connect(lambda text, _ms: app.statuses.append(text))
        win = MainWindow(app)
        self.qtbot.addWidget(win)
        win.resize(1100, 700)
        win.show()
        self.qtbot.waitExposed(win)
        if interval is not None:
            win.workspaces.set_autosave_interval(interval)
        self.win, self.app = win, app
        self.started.append(win)
        if decide:
            tabs = win.open_command_line([str(p) for p in command_line])
            self.decision = win.workspaces.at_start(tabs)
        return win

    def restart(self, **kw: Any) -> MainWindow:
        self.close()
        return self.start(**kw)

    def close(self) -> None:
        win, app = self.win, self.app
        if win is not None and not win._closing:
            win.close()
            assert win._closing
        if app is not None:
            assert app.tasks.wait_idle(30)
            assert app.errors == [], [repr(e) for e in app.errors]

    def saved(self) -> dict | None:
        return GuiState.load(self.state_path).get(ss.SESSION_KEY)


@pytest.fixture
def shell(qapp, qtbot, tmp_path, lang_uz):
    sh = Shell(qtbot, tmp_path)
    yield sh
    for win in sh.started:
        if not win._closing:
            win.close_without_questions()
    for w in list(widgets._kept_alive):
        w.close()


def restored(qtbot, win: MainWindow, fn) -> Any:
    """Run ``fn()`` and return the RestoreReport it leads to."""
    with qtbot.waitSignal(win.workspaces.restored, timeout=TIMEOUT) as blocker:
        fn()
    return blocker.args[0]


def open_ready(qtbot, win: MainWindow, path, options=None):
    tab = win.open_path(path, options)
    wait_ready(qtbot, tab.session)
    assert tab.session.state == "ready", tab.session.open_error
    return tab


def settle(qtbot, tab) -> None:
    wait_ready(qtbot, tab.session)
    qtbot.waitUntil(lambda: not tab.session.view_busy, timeout=TIMEOUT)


def tree_of(tab):
    pane = tab.showing("tree")
    assert pane is not None
    return pane.current_view()


def expand(qtbot, tree, kp: tuple) -> None:
    done: list[bool] = []
    tree.expand_node(kp, then=lambda: done.append(True), otherwise=lambda: done.append(False))
    qtbot.waitUntil(lambda: bool(done), timeout=TIMEOUT)
    assert done == [True], kp


def current_text(session) -> str | None:
    return session.location_text(session.current) if session.current is not None else None


def wait_expanded(qtbot, tree, *kps: tuple) -> None:
    qtbot.waitUntil(lambda: all(tree.flat.is_expanded(kp) for kp in kps), timeout=TIMEOUT)


# ----------------------------------------------------------------------------------------
# the round trip
# ----------------------------------------------------------------------------------------
def test_a_real_session_comes_back(qtbot, shell, tmp_path) -> None:
    path = people(tmp_path / "Тошкент ma'lumot.json")
    win = shell.start()
    assert shell.decision == "nothing" and shell.saved() is None
    tab = open_ready(qtbot, win, path)
    tree = tree_of(tab)
    expand(qtbot, tree, (3,))
    expand(qtbot, tree, (3, 2))                      # contact
    expand(qtbot, tree, (3, 2, 2))                   # contact.tags
    assert tree.expanded_locations() == ["#3", "#3.contact", "#3.contact.tags"]
    tab.panes[1].set_view("table")
    s = tab.session
    s.set_filter(FilterSpec(text="id > 2 and price == 1.10"))
    s.set_quick_filters({"name": "name != 'x'"})
    s.set_sort([SortKey("id", descending=True)])
    settle(qtbot, tab)
    s.select(NodeRef(3, ("contact", "city")), reveal=True)
    win.show_search("Тошкент")
    shell.close()
    saved = shell.saved()
    assert saved["tabs"][0]["expanded"] == ["#3", "#3.contact", "#3.contact.tags"]

    win2 = shell.start(decide=False)
    report = restored(qtbot, win2, lambda: win2.workspaces.at_start([]))
    assert report.source == "session" and report.missing == [] and len(report.opened) == 1
    assert shell.app.statuses[-1] == t("gui.sessionstore.restored", count=1)
    tab2 = win2.tab_widgets()[0]
    settle(qtbot, tab2)
    s2 = tab2.session
    assert s2.path == str(path) and [p.current_kind() for p in tab2.panes] == ["tree", "table"]
    assert s2.filter.text == "id > 2 and price == 1.10" and s2.quick_filters == {"name": "name != 'x'"}
    assert s2.sort_keys == [SortKey("id", descending=True)]
    assert [s2.record_at(p) for p in range(3)] == [59, 58, 57] and s2.view_len() == 57
    wait_expanded(qtbot, tree_of(tab2), (3,), (3, 2), (3, 2, 2))
    assert current_text(s2) == "#3.contact.city"
    assert win2.panels["search"].query_text() == "Тошкент"
    # the restored session is stored again (unchanged apart from its time)
    shell.close()
    again = shell.saved()
    assert again["tabs"] == saved["tabs"]


@pytest.mark.parametrize("kind", ["wrapped", "keyed", "jsonl", "single"])
def test_document_kinds_and_forced_options(qtbot, shell, tmp_path, kind: str) -> None:
    if kind == "wrapped":                 # two record arrays: "other" chosen by hand
        path = wrapped_file(tmp_path / "w.json", 20)
        options = {"record_path": "other"}
        nodes = [("wrapper",), ("wrapper", 0), (1,)]
        expected = ["$", "meta", "#1"]
        current = NodeRef(None, ("meta", "tags", 1))
    elif kind == "keyed":
        path = write_json(tmp_path / "k.json", {f"id{i}": {"n": i, "o": {"Тош": [i, i + 1]}} for i in range(15)})
        options = {}
        nodes = [(1,), (1, 1), (1, 1, 0)]
        expected = ["#1", "#1.o", "#1.o.Тош"]
        current = NodeRef(1, ("o", "Тош", 1))
    elif kind == "jsonl":                 # a JSON Lines file opened as concatenated values
        path = tmp_path / "l.jsonl"
        path.write_text("".join(json.dumps({"i": i, "a": [i, {"b": i}]}) + "\n" for i in range(12)),
                        encoding="utf-8")
        options = {"format": "stream"}
        nodes = [(2,), (2, 1), (2, 1, 1)]
        expected = ["#2", "#2.a", "#2.a[1]"]
        current = NodeRef(2, ("a", 1, "b"))
    else:
        path = write_json(tmp_path / "s.json", {"a": {"b": {"c": 1, "d": "Ўзбек"}}, "e": "O'zbek"})
        options = {}
        nodes = [("root", 0), ("root", 0, 0)]
        expected = ["a", "a.b"]
        current = NodeRef(None, ("a", "b", "d"))
    doc_kind = "stream" if kind == "jsonl" else kind
    win = shell.start()
    tab = open_ready(qtbot, win, path, options or None)
    assert tab.session.doc.kind == doc_kind
    tree = tree_of(tab)
    qtbot.waitUntil(lambda: tree.info_at(0) is not None, timeout=TIMEOUT)
    for kp in nodes:
        expand(qtbot, tree, kp)
    assert tree.expanded_locations() == expected
    tab.session.select(current, reveal=True)
    shell.close()
    win2 = shell.start(decide=False)
    restored(qtbot, win2, lambda: win2.workspaces.at_start([]))
    tab2 = win2.tab_widgets()[0]
    settle(qtbot, tab2)
    s2 = tab2.session
    assert s2.record_path == options.get("record_path") and s2.format == options.get("format")
    assert s2.doc.kind == doc_kind
    if kind == "wrapped":
        assert s2.doc.info.record_path_text == "other" and s2.doc.record_count == 3
    tree2 = tree_of(tab2)
    wait_expanded(qtbot, tree2, *nodes)
    qtbot.waitUntil(lambda: current_text(s2) == s2.location_text(current), timeout=TIMEOUT)


def test_tree_helpers_without_loaded_rows_and_with_bad_entries(qtbot, shell, tmp_path) -> None:
    """``expanded_locations`` names top-level records and array elements without their row data;
    ``restore_expanded`` skips what is gone, scalars and malformed texts, and takes at most ``limit``."""
    path = write_json(tmp_path / "arr.json", [[i, [i, {"k": [i]}], "Тошкент"] for i in range(8)])
    win = shell.start()
    tab = open_ready(qtbot, win, path)
    tree = tree_of(tab)
    qtbot.waitUntil(lambda: tree.info_at(0) is not None, timeout=TIMEOUT)
    for kp in ((5,), (5, 1), (5, 1, 1), (5, 1, 1, 0)):
        expand(qtbot, tree, kp)
    assert tree.expanded_locations() == ["#5", "#5[1]", "#5[1][1]", "#5[1][1].k"]
    assert tree.expanded_locations(limit=2) == ["#5", "#5[1]"]
    tree._loader.clear()                                   # the row data is gone (scrolled far away)
    tree._known.clear()
    assert tree.expanded_locations() == ["#5", "#5[1]", "#5[1][1]"]     # an object's member needs its key
    tree.collapse_all()
    got: list[int] = []
    assert tree.restore_expanded(["#999", "#5[0]", "#5", "bad[[", "#5[1]", "#5[1][1].k", 7], on_done=got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    assert got == [3] and tree.flat.expanded_keypaths() == [(5,), (5, 1), (5, 1, 1, 0)]
    tree.collapse_all()
    got.clear()
    assert tree.restore_expanded(["#1", "#2", "#3"], on_done=got.append, limit=2)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    assert got == [2] and tree.flat.expanded_keypaths() == [(1,), (2,)]


# ----------------------------------------------------------------------------------------
# missing and changed files
# ----------------------------------------------------------------------------------------
def test_missing_files_are_skipped_with_one_status_line(qtbot, shell, tmp_path) -> None:
    a = people(tmp_path / "a.json", 10)
    b = people(tmp_path / "b ko'cha.json", 10)
    c = people(tmp_path / "c.json", 10)
    win = shell.start()
    for p in (a, b, c):
        open_ready(qtbot, win, p)
    win.tabs.setCurrentIndex(1)
    shell.close()
    b.unlink()
    c.unlink()
    win2 = shell.start(decide=False)
    report = restored(qtbot, win2, lambda: win2.workspaces.at_start([]))
    assert report.missing == ["b ko'cha.json", "c.json"] and [t_.session.path for t_ in report.opened] == [str(a)]
    assert shell.app.statuses[-1] == (t("gui.sessionstore.restored", count=1) + " "
                                      + t("gui.sessionstore.missing", count=2, names="b ko'cha.json, c.json"))
    assert win2.current_tab() is report.opened[0]          # the active tab was missing: the one before it


def test_a_changed_file_reopens_normally(qtbot, shell, tmp_path) -> None:
    path = people(tmp_path / "a.json", 40)
    win = shell.start()
    tab = open_ready(qtbot, win, path)
    tree = tree_of(tab)
    expand(qtbot, tree, (2,))
    expand(qtbot, tree, (30,))
    expand(qtbot, tree, (30, 2))
    tab.session.select(NodeRef(30, ("contact", "zip")), reveal=True)
    shell.close()
    people(path, 10)                                        # fewer records now: #30 is gone
    win2 = shell.start(decide=False)
    restored(qtbot, win2, lambda: win2.workspaces.at_start([]))
    tab2 = win2.tab_widgets()[0]
    settle(qtbot, tab2)
    assert tab2.session.doc.record_count == 10 and tab2.info_bar("open_failed") is None
    tree2 = tree_of(tab2)
    wait_expanded(qtbot, tree2, (2,))
    qtbot.waitUntil(lambda: not win2.workspaces.restorer.pending_tabs() and not tree2.busy(), timeout=TIMEOUT)
    assert not tree2.flat.is_expanded((30,)) and shell.app.errors == []


# ----------------------------------------------------------------------------------------
# what is (not) restored
# ----------------------------------------------------------------------------------------
def test_search_results_are_not_restored_but_the_search_is(qtbot, shell, tmp_path) -> None:
    path = people(tmp_path / "a.json", 30)
    win = shell.start()
    tab = open_ready(qtbot, win, path)
    panel = win.show_search("Xiva")
    task = panel.run_search()
    assert task is not None
    qtbot.waitUntil(lambda: not panel.is_running(), timeout=TIMEOUT)
    with qtbot.waitSignal(tab.session.view_changed, timeout=TIMEOUT):
        panel.open_as_records()
    qtbot.waitUntil(lambda: tab.session.shown_records is not None, timeout=TIMEOUT)
    tab.session.set_filter(FilterSpec(text="id > 5"))
    settle(qtbot, tab)
    shell.close()
    saved = shell.saved()["tabs"][0]
    assert saved["filter"] is None and saved["sort"] == [] and saved["search"]["text"] == "Xiva"
    win2 = shell.start(decide=False)
    restored(qtbot, win2, lambda: win2.workspaces.at_start([]))
    tab2 = win2.tab_widgets()[0]
    settle(qtbot, tab2)
    assert tab2.session.view_is_base and tab2.session.filter is None
    assert win2.panels["search"].query_text() == "Xiva"


def test_unsaved_changes_come_back_only_through_the_recovery_bar(qtbot, shell, tmp_path) -> None:
    path = people(tmp_path / "a.json", 12)
    original = path.read_bytes()
    win = shell.start()
    tab = open_ready(qtbot, win, path)
    qtbot.waitUntil(lambda: tab.edit is not None and tab.edit.available and not tab.edit.busy, timeout=TIMEOUT)
    got: list = []
    tab.edit.set_value(NodeRef(1, ("name",)), json.dumps("tahrirlangan Ўзбек"), on_done=got.append)
    qtbot.waitUntil(lambda: bool(got) and tab.dirty, timeout=TIMEOUT)
    shell.close()                         # headless, no answer handler: the edit layer is kept for recovery
    assert path.read_bytes() == original
    win2 = shell.start(decide=False)
    restored(qtbot, win2, lambda: win2.workspaces.at_start([]))
    tab2 = win2.tab_widgets()[0]
    settle(qtbot, tab2)
    qtbot.waitUntil(lambda: tab2.info_bar("recovery") is not None, timeout=TIMEOUT)
    assert not tab2.dirty and tab2.session.doc.record(1)["name"] == "Oʻzbek 1"    # nothing restored silently
    assert tab2.edit is not None and tab2.edit.reason == "recovery_pending"


def test_expanded_nodes_wait_for_a_shown_tree(qtbot, shell, tmp_path) -> None:
    path = people(tmp_path / "a.json", 20)
    win = shell.start()
    tab = open_ready(qtbot, win, path)
    tree = tree_of(tab)
    expand(qtbot, tree, (4,))
    expand(qtbot, tree, (4, 2))
    tab.panes[0].set_view("table")        # the tree is no longer shown (its expansion is still kept)
    tab.set_active_pane(0)
    shell.close()
    assert shell.saved()["tabs"][0]["expanded"] == ["#4", "#4.contact"]
    assert shell.saved()["tabs"][0]["views"] == ["table", "text"]
    win2 = shell.start(decide=False)
    restored(qtbot, win2, lambda: win2.workspaces.at_start([]))
    tab2 = win2.tab_widgets()[0]
    settle(qtbot, tab2)
    restorer = win2.workspaces.restorer
    qtbot.wait(200)
    assert tab2 in restorer.pending_tabs() and tab2.panes[0].view("tree") is None
    with qtbot.waitSignal(restorer.expanded_restored, timeout=TIMEOUT) as blocker:
        win2.show_view_kind("tree")
    assert blocker.args == [tab2, 2]
    tree2 = tree_of(tab2)
    assert tree2.flat.is_expanded((4,)) and tree2.flat.is_expanded((4, 2))
    assert tab2 not in restorer.pending_tabs()


def test_a_capture_during_the_restore_keeps_what_is_still_coming(qtbot, shell, tmp_path) -> None:
    path = people(tmp_path / "a.json", 20)
    session = ss.SessionState(tabs=[ss.TabState(path=str(path), current="#4.contact", expanded=["#4", "#4.contact"],
                                                search={"text": "Xiva"})], active=0)
    state = GuiState.load(shell.state_path)
    ss.store_session(state, session)
    state.save(raise_errors=True)
    win = shell.start(decide=False)
    captured: list = []
    win.workspaces.restored.connect(lambda _r: captured.append(win.workspaces.capture()))
    restored(qtbot, win, lambda: win.workspaces.at_start([]))
    tab = captured[0].tabs[0]
    assert tab.expanded == ["#4", "#4.contact"] and tab.current == "#4.contact"
    assert tab.search["text"] == "Xiva"           # applied to the panel already (the tab is current)


# ----------------------------------------------------------------------------------------
# the start-up decision
# ----------------------------------------------------------------------------------------
def _saved_session(shell: Shell, *paths: Path) -> None:
    state = GuiState.load(shell.state_path)
    ss.store_session(state, ss.SessionState(tabs=[ss.TabState(path=str(p)) for p in paths], active=0))
    state.save(raise_errors=True)


def test_setting_never(qtbot, shell, tmp_path) -> None:
    a = people(tmp_path / "a.json", 5)
    _saved_session(shell, a)
    win = shell.start(mode="never")
    assert shell.decision == "never" and win.workspaces.bar is None
    qtbot.wait(200)
    assert win.tab_widgets() == []
    shell.close()
    assert shell.saved()["tabs"] == []                     # the session follows the window


def test_setting_ask_restore(qtbot, shell, tmp_path) -> None:
    a = people(tmp_path / "a.json", 5)
    b = people(tmp_path / "b.json", 5)
    _saved_session(shell, a, b)
    win = shell.start(mode="ask")
    assert shell.decision == "ask"
    bar = win.workspaces.bar
    assert bar is not None and bar.isVisible() and bar.message() == t("gui.sessionstore.ask", count=2)
    assert bar.detail() == "a.json, b.json" and win.tab_widgets() == []
    assert win.central.isVisible() and win.central.currentWidget() is win.welcome
    restore, skip = bar.buttons()
    assert (restore.text(), skip.text()) == (t("gui.sessionstore.restore"), t("gui.sessionstore.skip"))
    report = restored(qtbot, win, restore.click)
    assert [x.session.name for x in report.opened] == ["a.json", "b.json"]
    assert win.workspaces.bar is None and win.centralWidget() is win.central and win.central.isVisible()
    assert win.current_tab() is report.opened[0]


def test_setting_ask_no_and_unanswered(qtbot, shell, tmp_path) -> None:
    a = people(tmp_path / "a.json", 5)
    _saved_session(shell, a)
    win = shell.start(mode="ask")
    # not answered: closing keeps the saved session for the next start
    shell.close()
    assert [x["path"] for x in shell.saved()["tabs"]] == [str(a)]
    win = shell.start(mode="ask")
    bar = win.workspaces.bar
    bar.buttons()[1].click()                                # "Yo'q"
    assert win.workspaces.bar is None and win.centralWidget() is win.central
    qtbot.wait(100)
    assert win.tab_widgets() == []
    shell.close()
    assert shell.saved()["tabs"] == []                      # answered: the session follows the window
    _saved_session(shell, a)
    win = shell.start(mode="ask")
    win.workspaces.bar.close_bar()                          # closing the bar is "no" too
    assert win.workspaces.bar is None and not win.workspaces.keep_saved


def test_command_line_files_open_alone_and_keep_the_session(qtbot, shell, tmp_path) -> None:
    a = people(tmp_path / "a.json", 5)
    b = people(tmp_path / "b.json", 5)
    _saved_session(shell, a)
    win = shell.start(command_line=(b,))
    assert shell.decision == "command_line"
    assert [x.session.name for x in win.tab_widgets()] == ["b.json"]
    wait_ready(qtbot, win.tab_widgets()[0].session)
    assert not win.workspaces.store()                       # kept: not overwritten while this window lives
    shell.close()
    assert [x["path"] for x in shell.saved()["tabs"]] == [str(a)]
    # the next start without files restores it
    win2 = shell.start(decide=False)
    report = restored(qtbot, win2, lambda: win2.workspaces.at_start([]))
    assert [x.session.name for x in report.opened] == ["a.json"]


def test_command_line_without_a_saved_session_saves_its_own(qtbot, shell, tmp_path) -> None:
    b = people(tmp_path / "b.json", 5)
    win = shell.start(command_line=(b,))
    wait_ready(qtbot, win.tab_widgets()[0].session)
    shell.close()
    assert [x["path"] for x in shell.saved()["tabs"]] == [str(b)]


def test_the_autosave_timer(qtbot, shell, tmp_path) -> None:
    a = people(tmp_path / "a.json", 5)
    win = shell.start(interval=80)
    assert win.workspaces.timer.isActive() and win.workspaces.timer.interval() == 80
    with qtbot.waitSignal(win.workspaces.session_stored, timeout=TIMEOUT):
        open_ready(qtbot, win, a)
    qtbot.waitUntil(lambda: not shell.app.state_saving and bool((shell.saved() or {}).get("tabs")), timeout=TIMEOUT)
    assert [x["path"] for x in shell.saved()["tabs"]] == [str(a)]
    assert GuiState.load(shell.state_path).get("geometry")       # the window geometry too (crash safety)
    # nothing changed: nothing stored
    stored: list = []
    win.workspaces.session_stored.connect(lambda: stored.append(1))
    qtbot.wait(300)
    assert stored == []


def test_a_window_without_a_start_up_decision_writes_nothing(qtbot, shell, tmp_path) -> None:
    a = people(tmp_path / "a.json", 5)
    win = shell.start(decide=False)
    open_ready(qtbot, win, a)
    assert not win.workspaces.armed and not win.workspaces.timer.isActive()
    shell.close()
    assert shell.saved() is None


# ----------------------------------------------------------------------------------------
# app.main
# ----------------------------------------------------------------------------------------
@pytest.fixture
def clean_process(qapp, monkeypatch):
    """main() installs process-wide hooks and logging: restore them afterwards."""
    from jsonreshape import i18n
    from jsonreshape.gui import app as app_mod
    from jsonreshape.gui import theme

    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(app_mod, "GC_IGNORED_THREADS", app_mod.GC_IGNORED_THREADS + ("pytest_timeout",))
    old_lang = i18n.get_language()
    old_font = QFont(QApplication.font())
    old_switch = sys.getswitchinterval()
    gc_was_enabled = gc.isenabled()
    yield app_mod
    sys.setswitchinterval(old_switch)
    if gc_was_enabled:
        gc.enable()
    QApplication.setFont(old_font)
    from jsonreshape.log import shutdown_logging

    shutdown_logging()
    i18n.set_language(old_lang)
    theme.apply_theme(qapp, "system")
    theme._reset_for_tests()


def test_main_restores_or_keeps_the_session(qtbot, clean_process, tmp_path) -> None:
    app_mod = clean_process
    a = people(tmp_path / "a.json", 5)
    b = people(tmp_path / "b.json", 5)
    state = GuiState.load()
    ss.store_session(state, ss.SessionState(tabs=[ss.TabState(path=str(a))], active=0))
    state.save(raise_errors=True)
    win = app_mod.main([str(b)], run=False)
    qtbot.addWidget(win)
    try:
        assert [x.session.name for x in win.tab_widgets()] == ["b.json"] and win.workspaces.keep_saved
        wait_ready(qtbot, win.tab_widgets()[0].session)
    finally:
        win.close()
        assert win.app.tasks.wait_idle(30)
    assert [x["path"] for x in GuiState.load().get(ss.SESSION_KEY)["tabs"]] == [str(a)]
    win = app_mod.main([], run=False)
    qtbot.addWidget(win)
    try:
        qtbot.waitUntil(lambda: bool(win.tab_widgets()), timeout=TIMEOUT)
        assert [x.session.name for x in win.tab_widgets()] == ["a.json"]
        wait_ready(qtbot, win.tab_widgets()[0].session)
    finally:
        win.close()
        assert win.app.tasks.wait_idle(30)


# ----------------------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------------------
def test_the_settings_row(qtbot, shell) -> None:
    from jsonreshape.gui.settingsdialog import SettingsDialog

    win = shell.start()
    dlg = SettingsDialog(shell.app, win)
    qtbot.addWidget(dlg)
    combo = dlg.restore_combo
    assert [combo.itemData(i) for i in range(combo.count())] == list(ss.RESTORE_MODES)
    assert combo.itemText(1) == t("gui.settings.restore_session.ask") and dlg.restore_value() == "always"
    combo.setCurrentIndex(combo.findData("ask"))
    assert dlg.save()
    assert shell.app.state.get(ss.RESTORE_KEY) == "ask" and ss.restore_mode(shell.app.state) == "ask"
    again = SettingsDialog(shell.app, win)
    qtbot.addWidget(again)
    assert again.restore_value() == "ask"
    again.restore_defaults()
    assert again.restore_value() == "always"
    shell.app.state.set(ss.RESTORE_KEY, "odd")
    third = SettingsDialog(shell.app, win)
    qtbot.addWidget(third)
    assert third.restore_value() == "always"
    for d in (dlg, again, third):
        d.reject()
