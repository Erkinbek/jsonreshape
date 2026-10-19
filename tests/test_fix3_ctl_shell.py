"""Fix round 3, part 2 -- the main window (jsonreshape/gui/mainwindow.py; review3 M3, CL-4, L9 and the fix
agents' requests for ``_update_actions``).

* M3: Save, Save As, closing a tab and F5 commit the open inline editor first (real tree view) and the structure
  builder's rename (real MapView); an invalid value refuses with a status message and keeps the editor open.
* CL-4: closing a tab, F5 and closing the window ask "wait / stop / cancel" while a clean document's Save As runs.
* L9: Export has its own (theme-aware) toolbar icon; parentless error windows close with the window.
* The Edit menu shows the active view's keys and disables the type conversions §12.4 refuses.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from guishell_helpers import (
    TIMEOUT,
    EditTreeStub,
    edit_registry,
    install_stub_edit,
    make_app,
    stub_panels,
    stub_registry,
    wait_ready,
    write_json,
)
from jsonreshape import saver
from jsonreshape.errors import UserInputError
from jsonreshape.gui import editing, theme, widgets
from jsonreshape.gui.actions import change_type_refusal, with_keys
from jsonreshape.gui.doctab import DocumentTab, default_view_registry
from jsonreshape.gui.mainwindow import MainWindow
from jsonreshape.i18n import t

pytestmark = pytest.mark.gui

K = Qt.Key


# ----------------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------------
@pytest.fixture
def app(qapp, tmp_path, lang_uz):
    ctx = make_app(tmp_path)
    ctx.statuses = []
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    yield ctx
    for w in list(widgets._kept_alive):
        w.close()
    ctx.tasks.cancel_all()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def windows(qtbot, app):
    made: list[MainWindow] = []

    def make(registry=None) -> MainWindow:
        w = MainWindow(app, view_registry=registry if registry is not None else stub_registry(),
                       panel_factories=stub_panels())
        qtbot.addWidget(w)
        made.append(w)
        return w

    yield make
    for w in made:
        if not w._closing:
            w.close_without_questions()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def choices(monkeypatch):
    """``editing.ask_choice_handler``: answers by purpose, records every question."""
    state = {"answers": {}, "asked": []}

    def handler(_parent, purpose, name, options):
        state["asked"].append((purpose, name, tuple(options)))
        return state["answers"].get(purpose, "cancel")

    monkeypatch.setattr(editing, "ask_choice_handler", handler)
    return state


def people(path: Path, n: int = 6) -> Path:
    return write_json(path, [{"id": i, "name": f"n{i}", "g": i % 2} for i in range(n)])


def open_editable(qtbot, window: MainWindow, path: Path) -> DocumentTab:
    tab = window.open_path(path)
    wait_ready(qtbot, tab.session)
    assert tab.session.state == "ready", tab.session.open_error
    qtbot.waitUntil(lambda: tab.edit is not None and tab.edit.available and not tab.edit.busy, timeout=TIMEOUT)
    return tab


def read(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))


def saved_name(path: Path, index: int) -> object:
    """``read(path)[index]["name"]`` for polling while a save replaces the file: Windows refuses to open a file
    for the moment it is being replaced (PermissionError) -- that is "not saved yet", not a failure."""
    try:
        return read(path)[index]["name"]
    except (OSError, ValueError):
        return None


def tree_editor(qtbot, tab: DocumentTab, kp: tuple):
    """Open the real tree view's inline editor on ``kp`` (Enter) and return it."""
    tree = tab.active_view()
    assert type(tree).__name__ == "TreeView"
    qtbot.waitUntil(lambda: tree.row_of((kp[0],)) is not None, timeout=TIMEOUT)
    tree.expand_node((kp[0],))
    qtbot.waitUntil(lambda: tree.flat.is_expanded((kp[0],)) and not tree.is_loading((kp[0],)), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: tree.row_of(kp) is not None, timeout=TIMEOUT)
    row = tree.row_of(kp)
    qtbot.waitUntil(lambda: tree.cell_text(row, "key") is not None, timeout=TIMEOUT)
    tree.select_row(row, scroll="ensure")
    qtbot.waitUntil(lambda: tree.current_keypath() == kp, timeout=TIMEOUT)
    tree.grid.setFocus()
    QTest.keyClick(tree.grid, K.Key_Return)
    qtbot.waitUntil(lambda: tree.inline_editor() is not None, timeout=TIMEOUT)
    return tree.inline_editor()


def real_window(windows) -> MainWindow:
    w = windows(default_view_registry())
    w.resize(1200, 700)
    w.show()
    return w


def held_save(monkeypatch):
    gate = threading.Event()
    entered = threading.Event()
    real = saver.save

    def held(*a, **kw):
        entered.set()
        assert gate.wait(30)
        return real(*a, **kw)

    monkeypatch.setattr(saver, "save", held)
    return gate, entered


# ----------------------------------------------------------------------------------------
# M3: what is typed in an inline editor
# ----------------------------------------------------------------------------------------
def test_save_commits_the_open_inline_editor_first(qtbot, app, windows, tmp_path) -> None:
    path = people(tmp_path / "a.json")
    window = real_window(windows)
    tab = open_editable(qtbot, window, path)
    ed = tree_editor(qtbot, tab, (1, 1))
    ed.selectAll()
    QTest.keyClicks(ed, "TYPED")
    assert ed.text() == "TYPED" and not tab.dirty
    assert window.save_current()
    qtbot.waitUntil(lambda: saved_name(path, 1) == "TYPED", timeout=TIMEOUT)
    qtbot.waitUntil(lambda: tab.edit is not None and tab.edit.available and not tab.dirty, timeout=TIMEOUT)
    assert app.errors == []


def test_an_invalid_value_in_the_editor_refuses_the_save(qtbot, app, windows, tmp_path) -> None:
    path = people(tmp_path / "b.json")
    window = real_window(windows)
    tab = open_editable(qtbot, window, path)
    original = path.read_bytes()
    ed = tree_editor(qtbot, tab, (1, 0))                  # a number
    assert ed.kind == "number"
    ed.setText("12abc")
    assert not ed.is_valid()
    app.statuses.clear()
    assert window.save_current() is False
    assert window.close_tab() is False and not tab.closed
    assert app.statuses and app.statuses[-1] == t("gui.edit.editor.invalid", reason=ed.error_text())
    assert tab.active_view().inline_editor() is ed and not ed.done     # still open, the text kept
    assert ed.text() == "12abc" and path.read_bytes() == original and not tab.dirty
    ed.cancel()


def test_closing_a_tab_commits_the_editor_then_asks(qtbot, app, windows, tmp_path, monkeypatch) -> None:
    path = people(tmp_path / "c.json")
    asked: list = []
    monkeypatch.setattr(editing, "ask_save_handler",
                        lambda _p, name, options: asked.append((name, tuple(options))) or "save")
    window = real_window(windows)
    tab = open_editable(qtbot, window, path)
    ed = tree_editor(qtbot, tab, (2, 1))
    ed.selectAll()
    QTest.keyClicks(ed, "YOPILDI")
    window.close_tab()                                    # a clean document: the editor makes it dirty first
    qtbot.waitUntil(lambda: tab.closed, timeout=TIMEOUT)
    assert asked == [("c.json", ("save", "discard", "cancel"))]
    assert read(path)[2]["name"] == "YOPILDI"
    assert app.errors == []


def test_f5_commits_the_editor_then_asks(qtbot, app, windows, tmp_path, monkeypatch) -> None:
    path = people(tmp_path / "d.json")
    asked: list = []
    monkeypatch.setattr(editing, "ask_save_handler",
                        lambda _p, name, options: asked.append(name) or "save")
    window = real_window(windows)
    tab = open_editable(qtbot, window, path)
    ed = tree_editor(qtbot, tab, (3, 1))
    ed.selectAll()
    QTest.keyClicks(ed, "F5 dan oldin")
    window.reopen_current()
    qtbot.waitUntil(lambda: saved_name(path, 3) == "F5 dan oldin", timeout=TIMEOUT)
    assert asked == ["d.json"]
    qtbot.waitUntil(lambda: tab.session.state == "ready" and tab.edit is not None and tab.edit.available,
                    timeout=TIMEOUT)
    assert not tab.dirty and tab.session.doc.pinned().record(3)["name"] == "F5 dan oldin"


def test_save_commits_a_rename_of_the_structure_builder(qtbot, app, windows, tmp_path) -> None:
    from jsonreshape.gui.mapview import MapView

    registry = stub_registry()
    registry["mapping"] = MapView
    window = windows(registry)
    window.resize(1200, 700)
    window.show()
    tab = open_editable(qtbot, window, people(tmp_path / "m.json"))
    window.show_view_kind("mapping")
    v = tab.active_view()
    assert isinstance(v, MapView)
    qtbot.waitUntil(lambda: v.state.designer is not None, timeout=TIMEOUT)
    assert v.select_node(v.state.tree.find("[].name").id)
    v.tree.setFocus()
    QTest.keyClick(v.tree, K.Key_F2)
    qtbot.waitUntil(v.renaming, timeout=TIMEOUT)
    QTest.keyClicks(QApplication.focusWidget(), "ism")
    window.save_current()                                 # Ctrl+S: the rename being typed is committed first
    assert not v.renaming() and v.state.tree.find("[].ism") is not None


# ----------------------------------------------------------------------------------------
# CL-4: a busy but unchanged document
# ----------------------------------------------------------------------------------------
def test_closing_a_tab_while_its_save_as_runs_asks(qtbot, app, windows, tmp_path, monkeypatch, choices) -> None:
    gate, entered = held_save(monkeypatch)
    window = windows()
    tab = open_editable(qtbot, window, people(tmp_path / "h.json"))
    target = tmp_path / "h_copy.json"
    tab.edit.save_as(target)
    try:
        assert entered.wait(10)
        choices["answers"]["busy"] = "wait"
        assert window.close_tab() is False                 # asked; it closes once the Save As ended
        qtbot.waitUntil(lambda: bool(choices["asked"]), timeout=TIMEOUT)
        assert choices["asked"][-1] == ("busy", "h.json", ("wait", "stop", "cancel"))
        assert not tab.closed
        assert window.close_tab() is False and len(choices["asked"]) == 1     # its question is open already
    finally:
        gate.set()
    qtbot.waitUntil(lambda: tab.closed, timeout=TIMEOUT)
    assert target.exists() and read(target) == read(tmp_path / "h.json")
    assert app.errors == []


def test_quitting_while_a_save_as_runs_asks(qtbot, app, windows, tmp_path, monkeypatch, choices) -> None:
    gate, entered = held_save(monkeypatch)
    window = windows()
    window.show()
    tab = open_editable(qtbot, window, people(tmp_path / "q.json"))
    target = tmp_path / "q_copy.json"
    reports: list = []
    tab.edit.save_as(target, on_done=reports.append)
    try:
        assert entered.wait(10)
        choices["answers"]["busy"] = "cancel"
        window.close()
        qtbot.waitUntil(lambda: bool(choices["asked"]), timeout=TIMEOUT)
        qtbot.waitUntil(lambda: not window._close_chain, timeout=TIMEOUT)
        assert not window._closing and not tab.closed and window.isVisible()   # Cancel: the window stays
    finally:
        gate.set()
    qtbot.waitUntil(lambda: bool(reports), timeout=TIMEOUT)
    assert reports[0] is not None and target.exists()


def test_f5_while_a_save_as_runs_asks_and_stop_cancels_it(qtbot, app, windows, tmp_path, monkeypatch,
                                                           choices) -> None:
    gate, entered = held_save(monkeypatch)
    window = windows()
    tab = open_editable(qtbot, window, people(tmp_path / "f.json"))
    target = tmp_path / "f_copy.json"
    reports: list = []
    tab.edit.save_as(target, on_done=reports.append)
    try:
        assert entered.wait(10)
        choices["answers"]["busy"] = "stop"
        opens = tab.session._opens
        window.reopen_current()
        qtbot.waitUntil(lambda: bool(choices["asked"]), timeout=TIMEOUT)
    finally:
        gate.set()
    qtbot.waitUntil(lambda: bool(reports), timeout=TIMEOUT)
    assert reports == [None] and not target.exists()              # stopped: the copy is not written
    qtbot.waitUntil(lambda: tab.session._opens > opens and tab.session.state == "ready", timeout=TIMEOUT)


# ----------------------------------------------------------------------------------------
# L9
# ----------------------------------------------------------------------------------------
def test_export_has_its_own_theme_aware_icon(qtbot, qapp, windows) -> None:
    window = windows()

    def image(key: str):
        return window.action(key).icon().pixmap(24, 24).toImage()

    assert not window.action("export").icon().isNull()
    assert image("export") != image("save")
    try:
        window.set_theme("light")
        light = image("export")
        window.set_theme("dark")
        dark = image("export")
        assert light != dark                               # drawn in the theme's colors
    finally:
        window.set_theme("light")
        theme.apply_theme(qapp, "light")


def test_parentless_error_windows_close_with_the_window(qtbot, windows) -> None:
    window = windows()
    window.show()
    widgets.reset_error_limit()
    dialog = widgets.show_error(None, UserInputError("gui.edit.pending.gone"))
    assert dialog is not None and dialog.parent() is None and dialog.isVisible()
    window.close_without_questions()
    qtbot.waitUntil(lambda: not _visible(dialog), timeout=TIMEOUT)


def _visible(widget) -> bool:
    import shiboken6

    return shiboken6.isValid(widget) and widget.isVisible()


# ----------------------------------------------------------------------------------------
# the Edit menu: the view's keys, the type conversions §12.4 refuses
# ----------------------------------------------------------------------------------------
class KindsTreeStub(EditTreeStub):
    """A view with its own keys (no move keys, like the table) and the kinds of its selection."""

    edit_keys = {"move_up": None, "move_down": None}

    def change_type_kinds(self):
        return ("object",)


def test_edit_menu_follows_the_view_keys_and_type_refusals(qtbot, app, windows, tmp_path, monkeypatch) -> None:
    install_stub_edit(monkeypatch)
    registry = edit_registry()
    registry["tree"] = KindsTreeStub
    window = windows(registry)
    tab = window.open_path(people(tmp_path / "k.json"))
    wait_ready(qtbot, tab.session)
    qtbot.waitUntil(lambda: tab.edit is not None, timeout=TIMEOUT)
    window._update_actions()
    assert window.action("edit_move_up").text() == t("gui.actions.edit.move_up")          # no Alt+Up here
    assert window.action("edit_move_down").text() == t("gui.actions.edit.move_down")
    assert window.action("edit_duplicate").text() == with_keys(t("gui.actions.edit.duplicate"), "Ctrl+D")
    for kind, offered in (("number", False), ("bool", False), ("object", False), ("string", True),
                          ("array", True), ("null", True)):
        action = window.action(f"edit_type_{kind}")
        assert action.isEnabled() is offered, kind
        if not offered:
            assert action.toolTip() == change_type_refusal(("object",), kind), kind
    # another view: the keys come back
    registry_view = tab.active_view()
    registry_view.edit_keys = {}
    window._update_actions()
    assert window.action("edit_move_up").text() == with_keys(t("gui.actions.edit.move_up"), "Alt+Up")
