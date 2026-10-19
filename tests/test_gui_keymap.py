"""Keyboard settings (jsonreshape/gui/keymap.py + the Settings "Klaviatura" page; ARCHITECTURE §15.0).

Stable ids = the main window's ``_act`` keys, defaults = what ``_create_actions`` set (nothing stored: nothing
changes); overrides in ``gui_state["shortcuts"]`` applied at start and live, persisted, reset one / all; conflicts
refused with the other command's name (another action, the grids' fixed keys, the menu bar's Alt+letter, chords,
invalid keys) -- a command's own default keys always allowed; empty / corrupt stored values; other windows follow
``settings_changed``; random operations keep the invariants; the settings page round trip (filter, recorder,
"Tugmasiz", "Asl holiga", "Hammasini asl holiga", Save / Cancel) and the page without a main window.
"""

from __future__ import annotations

import json
import random

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication

from guishell_helpers import TIMEOUT, StubDialog, make_app, stub_panels, stub_registry
from jsonreshape.gui import keymap as K
from jsonreshape.gui.keymap import FIXED_KEYS, KeyMap, KeymapPage, normalize_key
from jsonreshape.gui.mainwindow import MainWindow
from jsonreshape.gui.palette import show_palette
from jsonreshape.gui.settingsdialog import SettingsDialog
from jsonreshape.i18n import MESSAGES, t

pytestmark = pytest.mark.gui

PORTABLE = QKeySequence.SequenceFormat.PortableText


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


def keys_of(window: MainWindow, ident: str) -> list[str]:
    return [s.toString(PORTABLE) for s in window.action(ident).shortcuts()]


def stored(state) -> dict | None:
    """What the GUI state holds under ``shortcuts`` (``None``: nothing; ``get`` would give the default ``{}``)."""
    return state.get(K.STATE_KEY) if K.STATE_KEY in state.keys() else None


def shown(qtbot, window: MainWindow) -> MainWindow:
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow, timeout=TIMEOUT)
    return window


def settings_dialog(qtbot, app, window) -> SettingsDialog:
    dlg = SettingsDialog(app, window)
    qtbot.addWidget(dlg)
    return dlg


def row_text(page: KeymapPage, ident: str, col: int) -> str:
    return page.table.item(page.ids().index(ident), col).text()


# ----------------------------------------------------------------------------------------
# key texts
# ----------------------------------------------------------------------------------------
def test_normalize_key(qapp) -> None:
    assert normalize_key("Ctrl+Alt+E") == "Ctrl+Alt+E"
    assert normalize_key("ctrl+shift+p") == "Ctrl+Shift+P"
    assert normalize_key("Num+*") == "*"
    assert normalize_key(QKeySequence(Qt.KeyboardModifier.KeypadModifier | Qt.Key.Key_Asterisk)) == "*"
    assert normalize_key(QKeySequence("Ctrl+F4")) == "Ctrl+F4"
    for bad in ("", "Ctrl+Bogus", "Ctrl+K, Ctrl+S", "Shift", "Ctrl", None, 5):
        assert normalize_key(bad) is None, bad
    assert K.native_text(["Ctrl+C", "Ctrl+Ins", "Copy"], keyboard_only=True) == "Ctrl+C, Ctrl+Ins"
    assert K.native_text(["Copy"], keyboard_only=True) == "Copy"


def test_gui_state_defaults_of_stage_6(tmp_path) -> None:
    from jsonreshape.gui.guistate import DEFAULTS, GuiState

    state = GuiState.load(tmp_path / "gui_state.json")
    assert state.get("shortcuts") == {} and state.get("palette_recent") == [] and state.get("restore_session") == \
        "always"
    state.get("shortcuts")["export"] = ["Ctrl+Alt+E"]           # a copy: the defaults table is never changed
    state.get("palette_recent").append("act:save")
    assert DEFAULTS["shortcuts"] == {} and DEFAULTS["palette_recent"] == [] and state.get("shortcuts") == {}
    assert "shortcuts" not in state.keys() and not state.dirty
    from jsonreshape.gui import sessionstore

    assert DEFAULTS[sessionstore.RESTORE_KEY] in sessionstore.RESTORE_MODES


def test_fixed_keys_are_valid_and_described(qapp) -> None:
    for fk in FIXED_KEYS:
        assert fk.what in MESSAGES and MESSAGES[fk.what]["uz"] and MESSAGES[fk.what]["en"], fk.what
        for key in fk.keys:
            assert normalize_key(key) is not None, key
            assert K.fixed_key(key) is not None, key
    # the grid keys of the spec (§15.0 / §12.9)
    for key in ("Up", "Down", "Left", "Right", "*", "Space", "Return", "Enter", "Shift+Return", "F2", "Ins", "Del",
                "Ctrl+D", "Alt+Up", "Alt+Down", "Ctrl+V", "Ctrl+C", "Ctrl+Shift+C", "Esc", "Tab", "Ctrl+A"):
        assert K.fixed_key(key) is not None, key
    assert K.fixed_key("Ctrl+Alt+E") is None and K.fixed_key("F5") is None


# ----------------------------------------------------------------------------------------
# the map
# ----------------------------------------------------------------------------------------
def test_defaults_are_what_the_window_set(qtbot, app, window) -> None:
    keymap = window.keymap
    assert isinstance(keymap, KeyMap) and K.keymap_of(window) is keymap
    ids = keymap.ids()
    assert ids[:3] == ["open", "reopen", "record_path"]           # menu order
    for ident in ("palette", "export", "copy_value", "zoom_in", "dock_tasks", "copy_more_value_json", "theme_dark"):
        assert ident in ids, ident
    # the view edit commands (display-only keys, handled by the grids) are not configurable
    assert not [i for i in ids if i.startswith("edit_")]
    assert keymap.set_keys("edit_edit_value", ["Ctrl+Alt+V"]) == t("gui.keymap.unknown", action="edit_edit_value")
    fresh = new_window(qtbot, app)
    for ident in ids:
        assert keymap.defaults(ident) == keys_of(fresh, ident) == keys_of(window, ident), ident
    fresh.close()
    assert keymap.defaults("export") == ["Ctrl+E"] and keymap.defaults("palette") == ["Ctrl+Shift+P"]
    assert keymap.defaults("close_tab") == ["Ctrl+W", "Ctrl+F4"]
    assert keymap.overrides() == {} and stored(app.state) is None
    # the defaults never conflict (else Qt would trigger neither)
    seen: dict[str, str] = {}
    for ident in ids:
        for key in keymap.defaults(ident):
            norm = normalize_key(key) or key
            assert seen.setdefault(norm, ident) == ident, (key, seen[norm], ident)
    keymap.apply()                                                    # applying nothing changes nothing
    assert all(keys_of(window, i) == keymap.defaults(i) for i in ids)


def test_override_applied_live_persisted_and_reset(qtbot, app, tmp_path) -> None:
    w = new_window(qtbot, app)
    try:
        shown(qtbot, w)
        keymap = w.keymap
        assert keymap.set_keys("palette", ["Ctrl+Alt+P"]) is None
        assert keys_of(w, "palette") == ["Ctrl+Alt+P"]
        assert w.action("palette").shortcut() == QKeySequence("Ctrl+Alt+P")      # the menu shows it
        assert stored(app.state) == {"palette": ["Ctrl+Alt+P"]}
        # the old key does nothing, the new one opens the palette
        focus = QApplication.focusWidget() or w
        qtbot.keyClick(focus, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        from jsonreshape.gui.palette import palette_of

        assert palette_of(w) is None or not palette_of(w).is_open()
        qtbot.keyClick(focus, Qt.Key.Key_P, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
        assert palette_of(w) is not None and palette_of(w).is_open()
        palette_of(w).close_palette()
        # no key at all, then several keys
        assert keymap.set_keys("export", []) is None
        assert keys_of(w, "export") == [] and stored(app.state)["export"] == []
        assert keymap.set_keys("export", ["Ctrl+Alt+E", "Ctrl+Shift+E"]) is None
        assert keys_of(w, "export") == ["Ctrl+Alt+E", "Ctrl+Shift+E"]
        # back to the defaults: the override is dropped (setting the defaults is the same)
        assert keymap.set_keys("export", ["Ctrl+E"]) is None
        assert "export" not in stored(app.state) and keys_of(w, "export") == ["Ctrl+E"]
        assert keymap.set_keys("goto", ["Ctrl+Alt+G"]) is None
        app.tasks.wait_idle(10)
        assert app.state.save()
    finally:
        w.close()
    # applied at start from the saved file
    saved = json.loads((tmp_path / "gui_state.json").read_text(encoding="utf-8"))["settings"][K.STATE_KEY]
    assert saved == {"goto": ["Ctrl+Alt+G"], "palette": ["Ctrl+Alt+P"]}
    ctx2 = make_app(tmp_path)
    try:
        w2 = new_window(qtbot, ctx2)
        assert keys_of(w2, "palette") == ["Ctrl+Alt+P"] and keys_of(w2, "goto") == ["Ctrl+Alt+G"]
        assert w2.keymap.defaults("palette") == ["Ctrl+Shift+P"]               # the defaults are still known
        assert w2.keymap.reset("palette") is None
        assert keys_of(w2, "palette") == ["Ctrl+Shift+P"] and stored(ctx2.state) == {"goto": ["Ctrl+Alt+G"]}
        w2.keymap.reset_all()
        assert keys_of(w2, "goto") == ["Ctrl+G"] and stored(ctx2.state) is None
        w2.close()
    finally:
        ctx2.tasks.cancel_all()
        assert ctx2.tasks.shutdown(30)


def test_conflicts_are_refused_with_the_other_name(qtbot, app, window) -> None:
    keymap = window.keymap
    save = window.action("save").text().replace("&", "")
    file_menu = t("gui.main.menu.file").replace("&", "")
    why = keymap.set_keys("export", ["Ctrl+S"])
    assert why == t("gui.keymap.conflict", keys="Ctrl+S", action=save, path=file_menu)
    # the grids' fixed keys (the description of what they do is named)
    for key, what in (("F2", "rename"), ("Del", "delete"), ("Return", "edit"), ("Enter", "edit"),
                      ("Shift+Return", "edit_json"), ("Ins", "insert"), ("Ctrl+D", "duplicate"),
                      ("Alt+Up", "move_node"), ("Ctrl+V", "paste"), ("Shift+Ins", "paste"), ("Ctrl+C", "copy_value"),
                      ("Ctrl+Shift+C", "copy_path"), ("Space", "full_value"), ("Esc", "cancel"), ("Tab", "focus"),
                      ("Up", "move"), ("PgDown", "page"), ("Shift+Down", "extend"), ("Ctrl+A", "select_all"),
                      ("*", "expand_all"), ("Num+*", "expand_all"), ("+", "expand_collapse")):
        why = keymap.set_keys("export", [key])
        assert why is not None and t(f"gui.keymap.fixed.{what}") in why, (key, why)
    assert "Alt+" in keymap.set_keys("export", ["Alt+Down"])
    # the menu bar's Alt+letter
    assert keymap.set_keys("export", ["Alt+F"]) == t("gui.keymap.menu_conflict", keys="Alt+F", menu=file_menu)
    # chords, invalid keys, a key twice
    assert keymap.set_keys("export", ["Ctrl+K, Ctrl+S"]) == t("gui.keymap.multi", keys="Ctrl+K, Ctrl+S")
    assert keymap.set_keys("export", ["Ctrl+Bogus"]) == t("gui.keymap.invalid", keys="Ctrl+Bogus")
    assert keymap.set_keys("export", [""]) is not None
    assert keymap.set_keys("export", ["Ctrl+Alt+1", "ctrl+alt+1"]) == t("gui.keymap.twice", keys="Ctrl+Alt+1")
    # nothing changed by the refusals
    assert keys_of(window, "export") == ["Ctrl+E"] and stored(app.state) is None
    # an action's own default keys are always allowed, even the fixed ones (the window-level forms)
    assert keymap.set_keys("expand_all", ["*"]) is None
    assert keymap.set_keys("copy_value", ["Ctrl+C"]) is None
    assert keymap.set_keys("copy_path", ["Ctrl+Shift+C", "Ctrl+Alt+Y"]) is None
    assert keys_of(window, "copy_path") == ["Ctrl+Shift+C", "Ctrl+Alt+Y"]
    assert window.action("copy_value").shortcuts()[0] == QKeySequence("Ctrl+C")


def test_moving_a_key_and_reset_conflicts(qtbot, app, window) -> None:
    keymap = window.keymap
    assert keymap.set_keys("save", ["Ctrl+Alt+S"]) is None          # frees Ctrl+S ...
    assert keymap.set_keys("export", ["Ctrl+S"]) is None             # ... so export may take it
    assert keys_of(window, "export") == ["Ctrl+S"] and keys_of(window, "save") == ["Ctrl+Alt+S"]
    export = window.action("export").text().replace("&", "")
    # save cannot get its default back while export has it
    assert keymap.reset("save") == t("gui.keymap.reset_conflict", keys="Ctrl+S", action=export)
    assert keys_of(window, "save") == ["Ctrl+Alt+S"]
    keymap.reset_all()
    assert keys_of(window, "export") == ["Ctrl+E"] and keys_of(window, "save") == ["Ctrl+S"]
    assert keymap.overrides() == {} and stored(app.state) is None


def test_empty_and_corrupt_stored_values(qtbot, app, tmp_path) -> None:
    state_file = tmp_path / "gui_state.json"
    for raw in ("buzuq", [1, 2], {}, None, 42):
        state_file.write_text(json.dumps({"version": 1, "settings": {K.STATE_KEY: raw}}), encoding="utf-8")
        ctx = make_app(tmp_path)
        try:
            w = new_window(qtbot, ctx)
            assert keys_of(w, "export") == ["Ctrl+E"] and keys_of(w, "save") == ["Ctrl+S"], raw
            assert w.keymap.overrides() == {}
            pal = show_palette(w)                      # the window works
            assert pal.is_open()
            pal.close_palette()
            assert w.keymap.set_keys("goto", ["Ctrl+Alt+G"]) is None   # a broken value is replaced by a clean one
            assert stored(ctx.state) == {"goto": ["Ctrl+Alt+G"]}
            w.close()
        finally:
            ctx.tasks.cancel_all()
            assert ctx.tasks.shutdown(30)
    raw_value = {"export": 5, "save": ["Ctrl+Bogus", "Ctrl+Alt+S"], "quit": ["F2"], "goto": "Ctrl+Alt+G",
                 "zz_future_action": ["Ctrl+Alt+U"], "reopen": [], "find_next": ["Ctrl+S"], "7": ["x"],
                 "bookmarks": ["Ctrl+Shift+D", "Ctrl+Alt+B", None, 3]}
    state_file.write_text(json.dumps({"version": 1, "settings": {K.STATE_KEY: raw_value}}), encoding="utf-8")
    ctx = make_app(tmp_path)
    try:
        w = new_window(qtbot, ctx)
        assert keys_of(w, "export") == ["Ctrl+E"]                 # not a list: ignored
        assert keys_of(w, "save") == ["Ctrl+Alt+S"]               # the invalid key dropped
        assert keys_of(w, "quit") == ["Ctrl+Q"]                   # nothing usable: the defaults stay
        assert keys_of(w, "goto") == ["Ctrl+Alt+G"]               # one text = one key
        assert keys_of(w, "reopen") == []                         # an empty list means no key
        assert keys_of(w, "find_next") == ["Ctrl+S"]              # save gave Ctrl+S away
        assert keys_of(w, "bookmarks") == ["Ctrl+Shift+D", "Ctrl+Alt+B"]
        # a later change keeps the entries of actions this version does not have
        assert w.keymap.set_keys("goto", ["Ctrl+Alt+H"]) is None
        kept = stored(ctx.state)
        assert kept["zz_future_action"] == ["Ctrl+Alt+U"] and kept["7"] == ["x"] and kept["goto"] == ["Ctrl+Alt+H"]
        assert "export" not in kept
        pal = show_palette(w)
        assert [e for e in pal.all_entries() if e.id == "act:goto"][0].keys == "Ctrl+Alt+H"
        pal.close_palette()
        w.close()
    finally:
        ctx.tasks.cancel_all()
        assert ctx.tasks.shutdown(30)


def test_other_windows_follow_settings_changed(qtbot, app) -> None:
    w1 = new_window(qtbot, app)
    w2 = new_window(qtbot, app)
    try:
        assert w1.keymap.set_keys("export", ["Ctrl+Alt+E"]) is None
        assert keys_of(w2, "export") == ["Ctrl+E"]
        app.settings_changed.emit()
        assert keys_of(w2, "export") == ["Ctrl+Alt+E"] and keys_of(w1, "export") == ["Ctrl+Alt+E"]
    finally:
        w1.close()
        w2.close()


def test_random_changes_keep_the_invariants(qtbot, app, window) -> None:
    keymap = window.keymap
    rng = random.Random(15)
    ids = keymap.ids()
    pool = ["Ctrl+Alt+" + c for c in "ABCDEFGHIJ"] + ["Ctrl+S", "Ctrl+E", "Ctrl+O", "F2", "Del", "Alt+F", "Alt+T",
                                                      "*", "Ctrl+C", "Ctrl+Shift+C", "F5", "F7", "Ctrl+K, Ctrl+S",
                                                      "Ctrl+Bogus", "Shift+F9", "Ctrl+1", "Ctrl+=", "Esc"]
    mnemonics = keymap._mnemonics()
    for step in range(300):
        ident = rng.choice(ids)
        before = {i: keys_of(window, i) for i in ids}
        stored_before = stored(app.state)
        op = rng.random()
        if op < 0.7:
            keys = rng.sample(pool, rng.choice((0, 1, 1, 1, 2)))
            why = keymap.set_keys(ident, keys)
        elif op < 0.95:
            why = keymap.reset(ident)
        else:
            keymap.reset_all()
            why = None
        if why is not None:                                   # a refusal changes nothing
            assert {i: keys_of(window, i) for i in ids} == before, step
            assert stored(app.state) == stored_before, step
        owners: dict[str, str] = {}
        for i in ids:
            own = {normalize_key(k) for k in keymap.defaults(i)}
            for key in keys_of(window, i):
                norm = normalize_key(key) or key
                assert owners.setdefault(norm, i) == i, (step, key, owners[norm], i)
                if norm not in own:
                    assert K.fixed_key(norm) is None and norm not in mnemonics, (step, i, key)
        assert (stored(app.state) or {}) == keymap.overrides(), step


# ----------------------------------------------------------------------------------------
# the settings page
# ----------------------------------------------------------------------------------------
def test_settings_page_round_trip(qtbot, app, window) -> None:
    dlg = settings_dialog(qtbot, app, window)
    page = dlg.keys_page
    tabs = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    assert tabs[-2:] == [t("gui.settings.tab.keys"), t("gui.settings.tab.advanced")]
    assert isinstance(page, KeymapPage) and page.keymap is window.keymap
    assert page.ids() == window.keymap.ids()
    headers = [page.table.horizontalHeaderItem(c).text() for c in range(4)]
    assert headers == [t("gui.keymap.col.action"), t("gui.keymap.col.path"), t("gui.keymap.col.default"),
                       t("gui.keymap.col.current")]
    assert row_text(page, "export", page.COL_ACTION) == window.action("export").text().replace("&", "")
    assert row_text(page, "export", page.COL_PATH) == t("gui.main.menu.file").replace("&", "")
    assert row_text(page, "export", page.COL_DEFAULT) == "Ctrl+E" == row_text(page, "export", page.COL_CURRENT)
    assert row_text(page, "copy_value", page.COL_DEFAULT) == "Ctrl+C, Ctrl+Ins"      # no hardware "Copy" key
    assert page.fixed_table.rowCount() == len(FIXED_KEYS)
    assert page.fixed_table.item(0, 1).text() == t(FIXED_KEYS[0].what)
    assert not page.reset_button("export").isEnabled()
    # record a new key for the selected command
    page.select("export")
    assert page.selected_id() == "export" and page.recorder.keySequence() == QKeySequence("Ctrl+E")
    qtbot.keyClick(page.recorder, Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    assert page.overrides() == {"export": ["Ctrl+Alt+E"]}
    assert row_text(page, "export", page.COL_CURRENT) == "Ctrl+Alt+E"
    assert page.table.item(page.ids().index("export"), page.COL_CURRENT).font().bold()
    assert page.reset_button("export").isEnabled()
    assert keys_of(window, "export") == ["Ctrl+E"]                   # applied only by "Saqlash"
    # refused keys name the reason; the recorder goes back to the current key
    qtbot.keyClick(page.recorder, Qt.Key.Key_F2)
    assert t("gui.keymap.fixed.rename") in page.message.text()
    assert page.recorder.keySequence() == QKeySequence("Ctrl+Alt+E") and page.overrides() == {"export": ["Ctrl+Alt+E"]}
    qtbot.keyClick(page.recorder, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert window.action("save").text().replace("&", "") in page.message.text()
    # "Tugmasiz" for another command
    page.select("goto")
    qtbot.mouseClick(page.none_button, Qt.MouseButton.LeftButton)
    assert page.overrides() == {"export": ["Ctrl+Alt+E"], "goto": []}
    assert row_text(page, "goto", page.COL_CURRENT) == t("gui.keymap.no_keys")
    # the global "restore defaults" leaves the keys alone
    dlg.restore_defaults()
    assert page.overrides() == {"export": ["Ctrl+Alt+E"], "goto": []}
    dlg.accept()
    assert keys_of(window, "export") == ["Ctrl+Alt+E"] and keys_of(window, "goto") == []
    assert stored(app.state) == {"export": ["Ctrl+Alt+E"], "goto": []}
    # opened again: the overrides are shown; "Asl holiga" of a row, then save
    dlg = settings_dialog(qtbot, app, window)
    page = dlg.keys_page
    assert page.overrides() == {"export": ["Ctrl+Alt+E"], "goto": []}
    assert row_text(page, "export", page.COL_CURRENT) == "Ctrl+Alt+E"
    qtbot.mouseClick(page.reset_button("export"), Qt.MouseButton.LeftButton)
    assert page.overrides() == {"goto": []} and row_text(page, "export", page.COL_CURRENT) == "Ctrl+E"
    dlg.accept()
    assert keys_of(window, "export") == ["Ctrl+E"] and stored(app.state) == {"goto": []}
    # "Hammasini asl holiga"
    dlg = settings_dialog(qtbot, app, window)
    qtbot.mouseClick(dlg.keys_page.reset_all_button, Qt.MouseButton.LeftButton)
    assert dlg.keys_page.overrides() == {}
    dlg.accept()
    assert keys_of(window, "goto") == ["Ctrl+G"] and stored(app.state) is None


def test_recording_replaces_the_keys_but_the_shown_key_changes_nothing(qtbot, app, window) -> None:
    dlg = settings_dialog(qtbot, app, window)
    page = dlg.keys_page
    page.select("close_tab")
    assert page.recorder.keySequence() == QKeySequence("Ctrl+W")
    qtbot.keyClick(page.recorder, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)     # the key it showed
    assert page.overrides() == {} and row_text(page, "close_tab", page.COL_CURRENT) == "Ctrl+W, Ctrl+F4"
    qtbot.keyClick(page.recorder, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    assert page.overrides() == {"close_tab": ["Ctrl+Alt+W"]}                       # one key replaces both
    assert t("gui.keymap.assigned", action=row_text(page, "close_tab", page.COL_ACTION), keys="Ctrl+Alt+W") \
        == page.message.text()
    # recording the default again: back to the defaults (no override)
    page.select("export")
    qtbot.keyClick(page.recorder, Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert page.overrides()["export"] == ["Ctrl+Shift+E"]
    qtbot.keyClick(page.recorder, Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier)
    assert "export" not in page.overrides()
    dlg.reject()


def test_settings_page_cancel_and_conflicting_reset(qtbot, app, window) -> None:
    dlg = settings_dialog(qtbot, app, window)
    page = dlg.keys_page
    assert page.assign("export", "Ctrl+Alt+E") is None
    dlg.reject()
    assert keys_of(window, "export") == ["Ctrl+E"] and stored(app.state) is None
    # a reset refused while another command's new key is the default
    dlg = settings_dialog(qtbot, app, window)
    page = dlg.keys_page
    assert page.assign("save", "Ctrl+Alt+S") is None
    assert page.assign("export", "Ctrl+S") is None
    why = page.reset("save")
    assert why == t("gui.keymap.reset_conflict", keys="Ctrl+S", action=window.action("export").text().replace("&", ""))
    assert why == page.message.text() and page.overrides() == {"save": ["Ctrl+Alt+S"], "export": ["Ctrl+S"]}
    assert page.clear_keys("export") is None
    assert page.reset("save") is None and page.overrides() == {"export": []}
    dlg.accept()
    assert keys_of(window, "save") == ["Ctrl+S"] and keys_of(window, "export") == []


def test_settings_page_filter(qtbot, app, window) -> None:
    dlg = settings_dialog(qtbot, app, window)
    page = dlg.keys_page
    for text in ("eksport", "EKSPORT", "экспорт", "ctrl+e"):
        page.filter_edit.setText(text)               # (QTest cannot type Cyrillic letters)
        visible = page.visible_ids()
        assert "export" in visible and "save" not in visible, (text, visible[:5])
        page.filter_edit.clear()
    assert page.visible_ids() == page.ids()
    page.set_filter(t("gui.main.menu.tools").replace("&", ""))        # by the menu path
    assert "compare_files" in page.visible_ids() and "export" not in page.visible_ids()
    page.set_filter("o'zgar")
    assert page.visible_ids()
    page.set_filter("zzqqxx")
    assert page.visible_ids() == []
    dlg.reject()


def test_settings_page_without_a_main_window(qtbot, app) -> None:
    dlg = SettingsDialog(app)
    qtbot.addWidget(dlg)
    page = dlg.keys_page
    assert page.keymap is None and page.ids() == [] and not page.table.isEnabled()
    assert page.message.text() == t("gui.keymap.unavailable")
    assert page.fixed_table.rowCount() == len(FIXED_KEYS)
    assert page.assign("export", "Ctrl+Alt+E") == t("gui.keymap.unavailable")
    assert not page.apply()
    assert dlg.save() and stored(app.state) is None


def test_with_the_session_restore_bar_shown(qtbot, app, tmp_path) -> None:
    """While the "restore the last session?" bar is shown the central widget is a holder (bar + document area):
    the map still finds every action, applies keys and the settings page works."""
    from jsonreshape.gui.sessionstore import SessionState, TabState

    w = new_window(qtbot, app)
    try:
        before = w.keymap.ids()
        bar = w.workspaces.show_bar(SessionState(tabs=[TabState(path=str(tmp_path / "x.json"))], active=0))
        assert bar is not None and w.centralWidget() is not w.central
        assert w.keymap.ids() == before
        assert w.keymap.set_keys("copy_value", ["Ctrl+Alt+C"]) is None     # a document-area action
        assert keys_of(w, "copy_value") == ["Ctrl+Alt+C"]
        assert w.action("copy_value").shortcutContext() == Qt.ShortcutContext.WidgetWithChildrenShortcut
        dlg = settings_dialog(qtbot, app, w)
        assert dlg.keys_page.keymap is w.keymap and dlg.keys_page.ids() == before
        assert dlg.keys_page.reset("copy_value") is None                     # its defaults back
        dlg.accept()
        assert keys_of(w, "copy_value")[0] == "Ctrl+C" and "copy_value" not in (stored(app.state) or {})
        w.workspaces.hide_bar()
        assert w.centralWidget() is w.central
    finally:
        w.close()


def test_settings_opened_from_the_window_menu(qtbot, app) -> None:
    panels = stub_panels()
    panels["settings"] = SettingsDialog                     # the real dialog, as Tools ▸ Settings opens it
    w = new_window(qtbot, app, panel_factories=panels)
    try:
        dlg = w.show_settings()
        assert isinstance(dlg, SettingsDialog) and dlg.keys_page.keymap is w.keymap
        assert dlg.keys_page.assign("dock_tasks", "Ctrl+Alt+T") is None
        dlg.accept()
        assert keys_of(w, "dock_tasks") == ["Ctrl+Alt+T"]
    finally:
        w.close()
