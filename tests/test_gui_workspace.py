"""Workspace files in the real window (jsonreshape/gui/workspace.py ``WorkspaceController``; docs/ARCHITECTURE.md
§15.0 "Workspace file"): File ▸ "Workspace'ni saqlash…" / "Workspace'ni ochish…" / "Oxirgi workspace'lar".

Saved in one window and opened in another one with its own (empty) GUI state -- another profile or machine:
the tabs come back with their views, filter / sort, expanded nodes and current node, the bookmarks of the
files and their table layouts, the structure builder's tree (an embedded mapping file: installed in the
builder, its edited state and original kept) and the window geometry. Paths relative to the workspace file
are tried first (a moved folder still works); unsaved changes are never copied (``"edits": "pending"`` only
informs, the recovery bar offers them). Opening with tabs open asks: close them (unsaved changes asked tab by
tab), add, or cancel. A corrupt file is refused with the JSON pointer of the bad part; an interrupted save
leaves the old file; the recent-workspaces menu."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from guishell_helpers import TIMEOUT, wait_ready, wrapped_file
from jsonreshape import fileio
from jsonreshape.gui import editing, mapmodel, widgets
from jsonreshape.gui import sessionstore as ss
from jsonreshape.gui import workspace as wsm
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.mainwindow import MainWindow
from jsonreshape.gui.session import FilterSpec, NodeRef
from jsonreshape.gui.workspace import WorkspaceError
from jsonreshape.i18n import t
from jsonreshape.sortkeys import SortKey

pytestmark = pytest.mark.gui


def people(path: Path, n: int = 30) -> Path:
    recs = [{"id": i, "name": f"Oʻzbek {i}" if i % 2 else f"Тошкент {i}",
             "contact": {"city": "Toshkent" if i % 3 else "Xiva", "zip": 100000 + i}, "price": "@P@"}
            for i in range(n)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(recs, ensure_ascii=False, indent=1).replace('"@P@"', "1.10"), encoding="utf-8")
    return path


class Windows:
    """Main windows, each on its own GUI state file (``profile``)."""

    def __init__(self, qtbot: Any, tmp_path: Path) -> None:
        self.qtbot = qtbot
        self.tmp_path = tmp_path
        self.made: list[MainWindow] = []

    def start(self, profile: str = "a") -> MainWindow:
        state = GuiState.load(self.tmp_path / f"state-{profile}.json")
        app = AppContext(state=state, headless=True)
        app.tasks.set_poll_interval(10)
        app.errors = []
        app.statuses = []
        app.error_reported.connect(app.errors.append)
        app.status_message.connect(lambda text, _ms: app.statuses.append(text))
        win = MainWindow(app)
        self.qtbot.addWidget(win)
        win.resize(1000, 650)
        win.show()
        self.qtbot.waitExposed(win)
        win.workspaces.at_start([])
        self.made.append(win)
        return win

    def close(self, win: MainWindow) -> None:
        if not win._closing:
            win.close()
        assert win.app.tasks.wait_idle(30)


@pytest.fixture
def windows(qapp, qtbot, tmp_path, lang_uz):
    ws = Windows(qtbot, tmp_path)
    yield ws
    for win in ws.made:
        if not win._closing:
            win.close_without_questions()
        assert win.app.tasks.wait_idle(30)
    for w in list(widgets._kept_alive):
        w.close()


def open_ready(qtbot, win: MainWindow, path, options=None):
    tab = win.open_path(path, options)
    wait_ready(qtbot, tab.session)
    assert tab.session.state == "ready", tab.session.open_error
    return tab


def settle(qtbot, tab) -> None:
    wait_ready(qtbot, tab.session)
    qtbot.waitUntil(lambda: not tab.session.view_busy, timeout=TIMEOUT)


def expand(qtbot, tree, kp: tuple) -> None:
    done: list[bool] = []
    tree.expand_node(kp, then=lambda: done.append(True), otherwise=lambda: done.append(False))
    qtbot.waitUntil(lambda: bool(done), timeout=TIMEOUT)
    assert done == [True], kp


def save(qtbot, win: MainWindow, path: Path) -> Path:
    with qtbot.waitSignal(win.workspaces.workspace_saved, timeout=TIMEOUT) as blocker:
        assert win.workspaces.save_workspace(path) is not None
    assert blocker.args == [str(path)]
    return path


def opened(qtbot, win: MainWindow, path: Path, **kw) -> Any:
    with qtbot.waitSignal(win.workspaces.restored, timeout=TIMEOUT,
                          check_params_cb=lambda report: report.source == "workspace") as blocker:
        assert win.workspaces.open_workspace(path, **kw) is not None
    return blocker.args[0]


# ----------------------------------------------------------------------------------------
# save / open
# ----------------------------------------------------------------------------------------
def test_save_and_open_in_another_profile(qtbot, windows, tmp_path) -> None:
    a = people(tmp_path / "loyiha" / "Тошкент ma'lumot.json")
    (tmp_path / "loyiha" / "sub").mkdir(parents=True)
    w = wrapped_file(tmp_path / "loyiha" / "sub" / "w.json", 12)
    win = windows.start("a")
    tab_a = open_ready(qtbot, win, a)
    tree = tab_a.showing("tree").current_view()
    expand(qtbot, tree, (2,))
    expand(qtbot, tree, (2, 2))
    s = tab_a.session
    s.set_filter(FilterSpec(text="id >= 2"))
    s.set_sort([SortKey("contact.city"), SortKey("id", descending=True)])
    settle(qtbot, tab_a)
    s.select(NodeRef(2, ("contact", "zip")), reveal=True)
    s.add_bookmark("Muhim ko'cha — Ўзбек", NodeRef(2, ("contact",)))
    layout = {"version": 2, "kind": "array", "record_path": None, "frozen": 1, "filter_row": False,
              "columns": [{"path": "name", "width": 222, "hidden": False}, {"path": "id", "width": 60, "hidden": True}]}
    win.state.set_table_layout(str(a), layout)
    tab_w = open_ready(qtbot, win, w, {"record_path": "other"})
    tab_w.panes[1].set_view("raw")
    win.tabs.setCurrentWidget(tab_a)
    target = save(qtbot, win, tmp_path / "loyiha" / "ish.jrworkspace")
    assert win.state.get(wsm.LAST_DIR_KEY) == str(tmp_path / "loyiha")
    assert wsm.recent_workspaces(win.state) == [str(target)]
    assert win.app.statuses[-1] == t("gui.workspace.saved", path=os.path.normpath(target))
    doc = json.loads(target.read_text(encoding="utf-8"))
    first, second = doc["tabs"]
    assert doc["active"] == 0 and doc["geometry"]["window"] and doc["geometry"]["state"]
    assert first["relpath"] == "Тошкент ma'lumot.json" and second["relpath"] == os.path.join("sub", "w.json")
    assert first["expanded"] == ["#2", "#2.contact"] and first["current"] == "#2.contact.zip"
    assert first["table_layout"] == layout and second["table_layout"] is None
    assert [b["name"] for b in first["bookmarks"]] == ["Muhim ko'cha — Ўзбек"]
    assert first["edits"] == "none" and first["mapping"] is None
    assert second["record_path"] == "other" and second["views"] == ["tree", "raw"]
    windows.close(win)

    other = windows.start("b")                     # another profile: nothing of the files known there
    other.resize(640, 480)
    report = opened(qtbot, other, target)
    assert report.source == "workspace" and report.missing == [] and len(report.opened) == 2
    assert other.app.statuses[-1] == t("gui.workspace.opened", name="ish.jrworkspace", count=2)
    assert other.size().height() == 650 and other.size().width() > 640     # the workspace geometry (screen-clamped)
    tab1, tab2 = other.tab_widgets()
    assert other.current_tab() is tab1
    settle(qtbot, tab1)
    s1 = tab1.session
    assert s1.filter.text == "id >= 2" and s1.sort_keys == [SortKey("contact.city"), SortKey("id", descending=True)]
    tree1 = tab1.showing("tree").current_view()
    qtbot.waitUntil(lambda: tree1.flat.is_expanded((2,)) and tree1.flat.is_expanded((2, 2)), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: s1.current is not None and s1.location_text(s1.current) == "#2.contact.zip",
                    timeout=TIMEOUT)
    assert [(b.name, b.record, b.path) for b in s1.bookmarks] == [("Muhim ko'cha — Ўзбек", 2, ["contact"])]
    assert other.state.table_layout(str(a)) == win.state.table_layout(str(a))
    settle(qtbot, tab2)
    assert tab2.session.record_path == "other" and tab2.session.doc.record_count == 3
    assert [p.current_kind() for p in tab2.panes] == ["tree", "raw"]
    assert wsm.recent_workspaces(other.state) == [str(target)] and other.workspaces.path == str(target)


def test_a_moved_workspace_folder_still_works(qtbot, windows, tmp_path) -> None:
    folder = tmp_path / "eski joy"
    a = people(folder / "data" / "a.json", 8)
    b = people(folder / "data" / "b.json", 8)
    win = windows.start()
    open_ready(qtbot, win, a)
    open_ready(qtbot, win, b)
    save(qtbot, win, folder / "w.jrworkspace")
    windows.close(win)
    moved = tmp_path / "yangi joy"
    os.rename(folder, moved)
    (moved / "data" / "b.json").unlink()
    again = windows.start("b")
    report = opened(qtbot, again, moved / "w.jrworkspace")
    assert [tab.session.path for tab in report.opened] == [str(moved / "data" / "a.json")]
    assert report.missing == ["b.json"]
    assert again.app.statuses[-1] == (t("gui.workspace.opened", name="w.jrworkspace", count=1) + " "
                                      + t("gui.sessionstore.missing", count=1, names="b.json"))


def test_the_builders_tree_is_embedded_and_restored(qtbot, windows, tmp_path) -> None:
    a = people(tmp_path / "a.json", 20)
    win = windows.start()
    tab = open_ready(qtbot, win, a)
    tab.panes[1].set_view("mapping")
    st = mapmodel.state_for(tab.session)
    qtbot.waitUntil(lambda: st.designer is not None and st.phase == "ready", timeout=TIMEOUT)
    res = st.run("rename", st.designer.tree.find("[].contact"), "manzil Ўзбек")
    assert res is not None and res.applied
    target = save(qtbot, win, tmp_path / "m.jrworkspace")
    doc = json.loads(target.read_text(encoding="utf-8"))
    mapping = doc["tabs"][0]["mapping"]
    assert mapping["version"] == 1 and mapping["input"]["source"] == str(a)
    assert "designer" in mapping                          # the original tree: "Asl holat" still works
    windows.close(win)

    again = windows.start("b")
    report = opened(qtbot, again, target)
    tab2 = report.opened[0]
    assert [p.current_kind() for p in tab2.panes] == ["tree", "mapping"]
    st2 = mapmodel.state_for(tab2.session)
    qtbot.waitUntil(lambda: st2.designer is not None and st2.designer.tree.find("[].manzil Ўзбек") is not None,
                    timeout=TIMEOUT)
    assert st2.source == "file" and st2.file_path is None and st2.can_reset
    assert st2.designer.tree.find("[].contact") is None
    qtbot.waitUntil(lambda: not again.workspaces.restorer.pending_tabs(), timeout=TIMEOUT)
    # "Asl holat" brings the inferred tree back
    st2.reset()
    assert st2.designer.tree.find("[].contact") is not None


def test_unsaved_changes_are_not_copied(qtbot, windows, tmp_path) -> None:
    a = people(tmp_path / "a.json", 10)
    original = a.read_bytes()
    win = windows.start()
    tab = open_ready(qtbot, win, a)
    qtbot.waitUntil(lambda: tab.edit is not None and tab.edit.available and not tab.edit.busy, timeout=TIMEOUT)
    got: list = []
    tab.edit.set_value(NodeRef(0, ("name",)), json.dumps("o'zgardi"), on_done=got.append)
    qtbot.waitUntil(lambda: bool(got) and tab.dirty, timeout=TIMEOUT)
    target = save(qtbot, win, tmp_path / "e.jrworkspace")
    assert json.loads(target.read_text(encoding="utf-8"))["tabs"][0]["edits"] == "pending"
    assert b"o'zgardi" not in target.read_bytes()
    windows.close(win)                                   # headless: the layer is kept for recovery
    assert a.read_bytes() == original
    again = windows.start("b")
    report = opened(qtbot, again, target)
    assert report.pending_edits == 1
    assert again.app.statuses[-1].endswith(t("gui.workspace.pending_edits", count=1))
    tab2 = report.opened[0]
    settle(qtbot, tab2)
    qtbot.waitUntil(lambda: tab2.info_bar("recovery") is not None, timeout=TIMEOUT)
    assert not tab2.dirty


# ----------------------------------------------------------------------------------------
# opening with tabs open
# ----------------------------------------------------------------------------------------
@pytest.fixture
def workspace_of_b(qtbot, windows, tmp_path) -> Path:
    b = people(tmp_path / "b.json", 6)
    win = windows.start("w")
    open_ready(qtbot, win, b)
    target = save(qtbot, win, tmp_path / "b.jrworkspace")
    windows.close(win)
    return target


def test_open_with_tabs_open_cancel_add_replace(qtbot, windows, tmp_path, workspace_of_b) -> None:
    a = people(tmp_path / "a.json", 6)
    win = windows.start()
    open_ready(qtbot, win, a)
    asked: list[int] = []
    choice = {"value": "cancel"}

    def ask(count, callback):
        asked.append(count)
        callback(choice["value"])

    win.workspaces.ask_open_mode = ask
    task = win.workspaces.open_workspace(workspace_of_b)
    qtbot.waitUntil(lambda: bool(asked) and task.state != "running", timeout=TIMEOUT)
    qtbot.wait(100)
    assert asked == [1] and [x.session.name for x in win.tab_widgets()] == ["a.json"]
    choice["value"] = "add"
    opened(qtbot, win, workspace_of_b)
    assert [x.session.name for x in win.tab_widgets()] == ["a.json", "b.json"] and asked == [1, 1]
    choice["value"] = "replace"
    report = opened(qtbot, win, workspace_of_b)
    assert asked == [1, 1, 2] and [x.session.name for x in win.tab_widgets()] == ["b.json"]
    assert win.tab_widgets() == report.opened


def test_replace_asks_about_unsaved_changes_tab_by_tab(qtbot, windows, tmp_path, workspace_of_b,
                                                       monkeypatch) -> None:
    a = people(tmp_path / "a.json", 6)
    win = windows.start()
    tab = open_ready(qtbot, win, a)
    qtbot.waitUntil(lambda: tab.edit is not None and tab.edit.available and not tab.edit.busy, timeout=TIMEOUT)
    got: list = []
    tab.edit.set_value(NodeRef(0, ("id",)), "99", on_done=got.append)
    qtbot.waitUntil(lambda: bool(got) and tab.dirty, timeout=TIMEOUT)
    win.workspaces.ask_open_mode = lambda _count, callback: callback("replace")
    questions: list[str] = []
    monkeypatch.setattr(editing, "ask_save_handler", lambda _p, name, _c: questions.append(name) or "cancel")
    task = win.workspaces.open_workspace(workspace_of_b)
    qtbot.waitUntil(lambda: bool(questions) and task.state != "running", timeout=TIMEOUT)
    qtbot.wait(100)
    assert questions == ["a.json"] and win.tab_widgets() == [tab] and tab.dirty      # cancelled: nothing opened
    monkeypatch.setattr(editing, "ask_save_handler", lambda _p, name, _c: questions.append(name) or "discard")
    report = opened(qtbot, win, workspace_of_b)
    assert [x.session.name for x in report.opened] == ["b.json"] and tab.closed
    assert a.read_text(encoding="utf-8").count('"id": 99') == 0


# ----------------------------------------------------------------------------------------
# errors, crash safety, menus
# ----------------------------------------------------------------------------------------
def test_a_corrupt_workspace_is_refused_with_the_pointer(qtbot, windows, tmp_path) -> None:
    a = people(tmp_path / "a.json", 4)
    target = tmp_path / "buzuq.jrworkspace"
    target.write_text(json.dumps({"version": 1, "tabs": [{"path": str(a), "expanded": ["#0", 5]}]}),
                      encoding="utf-8")
    win = windows.start()
    win.workspaces.open_workspace(target)
    qtbot.waitUntil(lambda: bool(win.app.errors), timeout=TIMEOUT)
    error = win.app.errors[0]
    assert isinstance(error, WorkspaceError) and error.key == "workspace.error.type"
    assert error.pointer == "/tabs/0/expanded/1" and "/tabs/0/expanded/1" in error.message
    win.app.errors.clear()
    qtbot.wait(100)
    assert win.tab_widgets() == [] and wsm.recent_workspaces(win.state) == []


def test_an_interrupted_save_keeps_the_old_file(qtbot, windows, tmp_path, monkeypatch) -> None:
    a = people(tmp_path / "a.json", 4)
    win = windows.start()
    open_ready(qtbot, win, a)
    target = save(qtbot, win, tmp_path / "w.jrworkspace")
    before = target.read_bytes()
    open_ready(qtbot, win, people(tmp_path / "b.json", 4))

    def crash(*_args, **_kw):
        raise OSError(5, "simulated crash")

    monkeypatch.setattr(fileio, "_replace", crash)
    win.workspaces.save_workspace(target)
    qtbot.waitUntil(lambda: bool(win.app.errors), timeout=TIMEOUT)
    assert target.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".w.jrworkspace")] == []     # no temp file
    win.app.errors.clear()


def test_save_without_tabs_and_the_action_state(qtbot, windows, tmp_path) -> None:
    win = windows.start()
    qtbot.waitUntil(lambda: not win.action("workspace_save").isEnabled(), timeout=TIMEOUT)
    assert win.workspaces.save_workspace(tmp_path / "x.jrworkspace") is None
    assert win.app.statuses[-1] == t("gui.workspace.empty") and not (tmp_path / "x.jrworkspace").exists()
    assert win.action("workspace_open").isEnabled()
    open_ready(qtbot, win, people(tmp_path / "a.json", 3))
    assert win.action("workspace_save").isEnabled()


def test_file_menu_dialogs_and_recent_workspaces(qtbot, windows, tmp_path) -> None:
    a = people(tmp_path / "a.json", 4)
    win = windows.start()
    file_menu = win.menus["file"]
    names = [x.objectName() for x in file_menu.actions()]
    assert names.index("action_workspace_open") < names.index("action_workspace_save") < names.index("action_save")
    assert win.menus["recent_workspaces"].menuAction() in file_menu.actions()
    open_ready(qtbot, win, a)
    asked: list[tuple] = []
    win._ask_save_path = lambda title, default, filters: asked.append((title, default, filters)) or str(
        tmp_path / "tanlangan")
    with qtbot.waitSignal(win.workspaces.workspace_saved, timeout=TIMEOUT):
        win.action("workspace_save").trigger()
    title, default, filters = asked[0]
    assert title == t("gui.workspace.save.title") and default == str(tmp_path / "a.jrworkspace")
    assert "*.jrworkspace" in filters
    saved = tmp_path / "tanlangan.jrworkspace"                         # the extension is added
    assert saved.exists()
    # the recent menu lists it; its entry opens it (here: added to the open tab)
    menu = win.menus["recent_workspaces"]
    menu.aboutToShow.emit()
    entries = [x for x in menu.actions() if x.objectName().startswith("recent_workspace_")]
    assert [x.text() for x in entries] == ["&1 tanlangan.jrworkspace"] and entries[0].toolTip() == str(saved)
    win.workspaces.ask_open_mode = lambda _count, callback: callback("add")
    with qtbot.waitSignal(win.workspaces.restored, timeout=TIMEOUT):
        entries[0].trigger()
    assert len(win.tab_widgets()) == 2
    # the open dialog starts in the last workspace folder
    seen: list[tuple] = []
    win._ask_open_path = lambda title, folder, filters: seen.append((title, folder)) or None
    assert win.workspaces.open_workspace() is None
    assert seen == [(t("gui.workspace.open.title"), str(tmp_path))]
    # a recent file that is gone is dropped with a status line
    saved.unlink()
    menu.aboutToShow.emit()
    entry = [x for x in menu.actions() if x.objectName() == "recent_workspace_1"][0]
    entry.trigger()
    qtbot.waitUntil(lambda: wsm.recent_workspaces(win.state) == [], timeout=TIMEOUT)
    assert win.app.statuses[-1] == t("gui.workspace.recent.missing", path=os.path.normpath(saved))
    assert win.app.errors == []
    # clear
    wsm.add_recent_workspace(win.state, str(tmp_path / "x.jrworkspace"))
    menu.aboutToShow.emit()
    clear = [x for x in menu.actions() if x.objectName() == "recent_workspaces_clear"][0]
    assert clear.isEnabled()
    clear.trigger()
    assert wsm.recent_workspaces(win.state) == []
    menu.aboutToShow.emit()
    assert [x.text() for x in menu.actions()][0] == t("gui.workspace.recent.empty")


def test_opening_a_workspace_answers_the_restore_question(qtbot, windows, tmp_path, workspace_of_b) -> None:
    a = people(tmp_path / "a.json", 4)
    state = GuiState.load(tmp_path / "state-q.json")
    state.set(ss.RESTORE_KEY, "ask")
    ss.store_session(state, ss.SessionState(tabs=[ss.TabState(path=str(a))], active=0))
    state.save(raise_errors=True)
    win = windows.start("q")
    assert win.workspaces.bar is not None and win.workspaces.keep_saved
    opened(qtbot, win, workspace_of_b)
    assert win.workspaces.bar is None and not win.workspaces.keep_saved and win.centralWidget() is win.central
    assert [x.session.name for x in win.tab_widgets()] == ["b.json"]
