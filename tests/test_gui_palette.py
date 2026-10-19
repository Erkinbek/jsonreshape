"""The command palette (jsonreshape/gui/palette.py; ARCHITECTURE §15.0, AGENT_PROMPT §11 "Buyruqlar palitrasi").

Entries walked from the menu bar (stage-5 Tools items and actions added later included, texts without ``&``, menu
paths, current keys, display-only keys of the view edit commands), the current view's edit commands and the current
node's context-menu commands (no duplicates of the Edit menu), "Tab: …" and "Ochish: …"; matching like the search
(case, apostrophe variants, Latin <-> Cyrillic) and the ranking (exact prefix > word start > initials > inside >
subsequence; words matched in the menu path; ties: enabled, recent, shorter); disabled entries listed with their
reason and never run (Enter, click); Enter runs after the popup closed with the focus back; Esc, the list keys, a
click outside; the recent list (persisted, capped, broken values ignored); Ctrl+Shift+P; speed with ~300 entries.
"""

from __future__ import annotations

import os
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence, QPalette
from PySide6.QtWidgets import QApplication, QMenu

from guishell_helpers import (
    TIMEOUT,
    EditTreeStub,
    StubDialog,
    edit_registry,
    install_stub_edit,
    make_app,
    records,
    stub_panels,
    stub_registry,
    wait_ready,
    write_json,
)
from jsonreshape.gui import palette as P
from jsonreshape.gui import theme
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.mainwindow import MainWindow
from jsonreshape.gui.palette import PaletteEntry, rank
from jsonreshape.gui.session import NodeRef
from jsonreshape.i18n import t

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


def shown(qtbot, window: MainWindow) -> MainWindow:
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow, timeout=TIMEOUT)
    return window


def open_ready(qtbot, window: MainWindow, path):
    tab = window.open_path(path)
    wait_ready(qtbot, tab.session)
    assert tab.session.state == "ready", tab.session.open_error
    return tab


def entry(pal: P.CommandPalette, ident: str) -> PaletteEntry:
    found = [e for e in pal.all_entries() if e.id == ident]
    assert found, (ident, [e.id for e in pal.all_entries()])
    return found[0]


def ids(pal: P.CommandPalette) -> list[str]:
    return [e.id for e in pal.entries()]


def E(text: str, path: str = "", *, ident: str | None = None, enabled: bool = True, order: int = 0) -> PaletteEntry:
    return PaletteEntry(id=ident or f"x:{text}", text=text, path=path, enabled=enabled, order=order,
                        run=lambda: None, reason="" if enabled else "sabab")


# ----------------------------------------------------------------------------------------
# text helpers and ranking (pure)
# ----------------------------------------------------------------------------------------
def test_clean_text_and_keys(qapp) -> None:
    assert P.clean_text("&Fayl") == ("Fayl", "")
    assert P.clean_text("Save && &Close") == ("Save & Close", "")
    assert P.clean_text("Qiymatni tahrirlash\tEnter") == ("Qiymatni tahrirlash", "Enter")
    assert P.clean_text("&1 data && co.json") == ("1 data & co.json", "")
    action = QAction("&Ochish…")
    action.setShortcuts(QKeySequence.keyBindings(QKeySequence.StandardKey.Open))
    assert P.display_keys(action) == "Ctrl+O"            # the hardware "Open" key is not shown
    action.setShortcuts([QKeySequence("Ctrl+W"), QKeySequence("Ctrl+F4")])
    assert P.display_keys(action) == "Ctrl+W, Ctrl+F4"
    shown_only = QAction("Qiymatni tahrirlash\tShift+Enter")
    assert P.display_keys(shown_only) == "Shift+Enter"
    assert P.is_keyboard_key(QKeySequence("F5")) and P.is_keyboard_key(QKeySequence("*"))
    assert P.is_keyboard_key(QKeySequence("Del")) and not P.is_keyboard_key(QKeySequence(Qt.Key.Key_Copy))


def test_ranking_tiers() -> None:
    entries = [E("Dastur haqida", order=0), E("O'chirish", order=1), E("Boshqacha saqlash…", order=2),
               E("Saqlash", order=3), E("Ommaviy amallar…", order=4), E("Hissa", order=5)]
    texts = [r.entry.text for r in rank(entries, "saq")]
    # prefix > word start > subsequence (Dastur haqida: s-a-q in order)
    assert texts == ["Saqlash", "Boshqacha saqlash…", "Dastur haqida"]
    keys = {r.entry.text: r.key[0] for r in rank(entries, "saq")}
    assert keys == {"Saqlash": P.TIER_PREFIX, "Boshqacha saqlash…": P.TIER_WORD, "Dastur haqida": P.TIER_FUZZY}
    # initials of words before a match inside a word
    entries = [E("Tabsiz", order=0), E("Boshqacha saqlash", order=1)]
    assert [r.entry.text for r in rank(entries, "bs")] == ["Boshqacha saqlash", "Tabsiz"]
    assert [r.key[0] for r in rank(entries, "bs")] == [P.TIER_INITIALS, P.TIER_INSIDE]
    # inside a word ranks after word starts; the earlier match first
    entries = [E("Nusxalash", order=0), E("Asl nusxa", order=1), E("Tasnif", order=2)]
    assert [r.entry.text for r in rank(entries, "as")] == ["Asl nusxa", "Tasnif", "Nusxalash"]
    assert [r.key[0] for r in rank(entries, "as")] == [P.TIER_PREFIX, P.TIER_INSIDE, P.TIER_INSIDE]
    assert rank(entries, "zzz") == []
    assert rank([], "a") == []


def test_matching_is_case_apostrophe_and_script_insensitive() -> None:
    entries = [E("O'zgarishlarni JSON Patch sifatida saqlash…", order=0), E("Barcha oʻzgarishlarni tashlash", order=1),
               E("Saqlash", order=2), E("Eksport…", order=3), E("Ochish: Тошкент.json", order=4),
               E("Ishoq ro'yxati", order=5)]
    for query in ("o'zgar", "oʻzgar", "o‘zgar", "o’zgar", "o`zgar", "O'ZGAR", "ўзгар", "ЎЗГАР"):
        found = [r.entry.text for r in rank(entries, query)]
        assert found[:2] == ["O'zgarishlarni JSON Patch sifatida saqlash…", "Barcha oʻzgarishlarni tashlash"], query
    for query in ("сақлаш", "САҚЛАШ", "saqlash", "SAQLASH"):
        assert rank(entries, query)[0].entry.text == "Saqlash", query
    # Cyrillic е inside a word reads "e" (after a consonant): "експорт" finds "Eksport"
    assert rank(entries, "експорт")[0].entry.text == "Eksport…"
    assert rank(entries, "экспорт")[0].entry.text == "Eksport…"
    # Latin finds Cyrillic text
    assert rank(entries, "toshkent")[0].entry.text == "Ochish: Тошкент.json"
    assert rank(entries, "тошкент")[0].entry.text == "Ochish: Тошкент.json"
    # the apostrophe between s and h is optional (Is'hoq / Ishoq)
    assert rank(entries, "is'hoq")[0].entry.text == "Ishoq ro'yxati"
    # matched characters (original indices) for the highlight
    hit = rank(entries, "oʻzgar")[1]
    assert hit.entry.text == "Barcha oʻzgarishlarni tashlash"
    assert "".join(hit.entry.text[i] for i in hit.hits) == "oʻzgar"


def test_several_words_and_menu_path() -> None:
    entries = [E("Saqlash", "Fayl", order=0), E("Boshqacha saqlash…", "Fayl", order=1),
               E("JSON Patch sifatida saqlash", "Tahrir", order=2), E("Ochish…", "Fayl", order=3)]
    # the whole query as a prefix first
    assert rank(entries, "boshqacha saq")[0].entry.text == "Boshqacha saqlash…"
    assert rank(entries, "boshqacha saq")[0].key[0] == P.TIER_PREFIX
    # word by word: "fayl" in the path, "saq" in the text
    assert [r.entry.text for r in rank(entries, "fayl saq")] == ["Saqlash", "Boshqacha saqlash…"]
    # a single word found only in the path ranks after text matches
    entries.append(E("Tahrirlash rejimi", "Ko'rinish", order=4))
    found = [r.entry.text for r in rank(entries, "tahrir")]
    assert found == ["Tahrirlash rejimi", "JSON Patch sifatida saqlash"]
    # VS Code's ">" prefix is ignored
    found = rank(entries, "> ochish")
    assert found[0].entry.text == "Ochish…" and found[0].key[0] == P.TIER_PREFIX


def test_ties_enabled_recent_shorter_order() -> None:
    entries = [E("Saqlash", ident="a", enabled=False, order=0), E("Saqlash", ident="b", order=1),
               E("Saqlash va chiqish", ident="d", order=2), E("Saqlash va yopish", ident="c", order=3)]
    # same tier: the enabled ones first, then the shorter text, then the menu order
    assert [r.entry.id for r in rank(entries, "saq")] == ["b", "c", "d", "a"]
    # a recently used command comes before the shorter ones of its tier
    assert [r.entry.id for r in rank(entries, "saq", ["d"])] == ["d", "b", "c", "a"]
    assert [r.recent for r in rank(entries, "saq", ["d"])] == [True, False, False, False]
    # a better tier still wins over a recent one
    assert [r.entry.id for r in rank(entries + [E("Avto saqlash", ident="e", order=4)], "saq", ["e"])][:1] == ["b"]


def test_empty_query_lists_recent_first() -> None:
    entries = [E(f"Buyruq {i}", ident=f"id{i}", order=i) for i in range(30)]
    recent = ["id7", "missing", "id3", "id7", "id12"]
    ordered = [r.entry.id for r in rank(entries, "", recent)]
    assert ordered[:3] == ["id7", "id3", "id12"]
    assert ordered[3:] == [f"id{i}" for i in range(30) if i not in (7, 3, 12)]
    assert [r.recent for r in rank(entries, "", recent)][:4] == [True, True, True, False]
    assert [r.entry.id for r in rank(entries, "   ", [])] == [f"id{i}" for i in range(30)]
    # at most RECENT_MAX recent ones
    many = [f"id{i}" for i in range(25)]
    assert sum(r.recent for r in rank(entries, "", many)) == P.RECENT_MAX


def test_remember_and_recent_ids(tmp_path) -> None:
    state = GuiState.load(tmp_path / "s.json")
    assert P.recent_ids(state) == []
    state.set(P.STATE_RECENT, "buzuq")
    assert P.recent_ids(state) == []
    assert P.remember(state, "act:save") == ["act:save"]
    state.set(P.STATE_RECENT, [1, None, "act:open", {"x": 1}])
    assert P.recent_ids(state) == ["act:open"]
    for i in range(30):
        P.remember(state, f"act:{i}")
    P.remember(state, "act:5")
    got = state.get(P.STATE_RECENT)
    assert len(got) == P.RECENT_MAX and got[0] == "act:5" and got.count("act:5") == 1 and got[1] == "act:29"


def test_ranking_speed_many_entries() -> None:
    words = ["saqlash", "ochish", "eksport", "filtr", "tartiblash", "o'zgartirish", "qidiruv", "nusxalash",
             "ko'rinish", "statistika", "taqqoslash", "birlashtirish"]
    entries = [E(f"{words[i % 12].capitalize()} {words[(i * 7) % 12]} {i}", f"Menyu {i % 9} ▸ Bo'lim {i % 5}",
                 order=i) for i in range(2000)]
    rank(entries, "a")                       # fold cache
    for query in ("s", "sa", "saq", "saql", "o'zg", "ко'р", "qid nus", "xyz"):
        start = time.perf_counter()
        rank(entries, query)
        assert time.perf_counter() - start < 0.25, query     # ~10 ms here; generous for slow machines


# ----------------------------------------------------------------------------------------
# entries of a window
# ----------------------------------------------------------------------------------------
def walk(window: MainWindow) -> list[tuple[QAction, tuple[str, ...]]]:
    out = []

    def rec(menu: QMenu, path: tuple[str, ...]) -> None:
        for action in menu.actions():
            if action.isSeparator() or not action.isVisible():
                continue
            if action.menu() is not None:
                rec(action.menu(), path + (P.clean_text(action.text())[0],))
            elif P.clean_text(action.text())[0]:
                out.append((action, path))

    for top in window.menuBar().actions():
        rec(top.menu(), (P.clean_text(top.text())[0],))
    return out


def test_palette_lists_every_menu_action(qtbot, window, data_file) -> None:
    open_ready(qtbot, window, data_file)
    pal = window.show_palette()
    assert pal is P.palette_of(window) and pal.is_open()
    by_action = {id(e.action): e for e in pal.all_entries() if e.action is not None}
    recent_menu = window.menus["recent"]
    for action, path in walk(window):
        if action in recent_menu.actions() and action.objectName() != "recent_clear":
            continue                                  # the recent files are the "Ochish: …" entries
        if not action.isEnabled() and not action.objectName() and not action.shortcuts():
            assert P.clean_text(action.text())[0] not in [e.text for e in pal.all_entries()]
            continue                                  # a placeholder ("Ro'yxat bo'sh"), not a command
        e = by_action.get(id(action))
        assert e is not None, (path, action.text())
        assert e.text == P.clean_text(action.text())[0] and "&" not in e.text.replace("&&", "")
        assert e.path == P.PATH_SEP.join(path)
    # ids of window actions are their _act keys; the stage-5 Tools items are there with their paths
    tools = t("gui.main.menu.tools").replace("&", "")
    for key in ("compare_files", "stats", "dupes", "repair", "split_file", "merge_files", "settings"):
        assert entry(pal, f"act:{key}").path == tools, key
    schema = entry(pal, "act:schema_generate")
    assert schema.path == P.PATH_SEP.join([tools, "JSON Schema"])
    # current keys; the Edit menu's view commands show their display-only keys
    assert entry(pal, "act:save").keys == "Ctrl+S"
    assert entry(pal, "act:open").keys == "Ctrl+O"
    assert entry(pal, "act:close_tab").keys == "Ctrl+W, Ctrl+F4"
    assert entry(pal, "act:palette").keys == "Ctrl+Shift+P"
    assert entry(pal, "act:edit_edit_value").keys == "Enter"
    assert entry(pal, "act:edit_rename").keys == "F2"
    # checkable actions carry their state; tabs and recent files are there
    assert entry(pal, "act:second_pane").checked is True
    assert entry(pal, "act:theme_system").checked is not None
    norm = os.path.normcase(os.path.abspath(str(data_file)))
    tab_entry = entry(pal, f"tab:{norm}")
    assert tab_entry.text == t("gui.palette.tab", name="a.json") and tab_entry.checked is True
    assert entry(pal, f"open:{norm}").text == t("gui.palette.open", name="a.json")
    assert entry(pal, f"open:{norm}").path == os.path.normpath(str(data_file))
    # nothing twice
    assert len({e.id for e in pal.all_entries()}) == len(pal.all_entries())


@pytest.fixture
def data_file(tmp_path):
    return write_json(tmp_path / "a.json", records(20))


def test_actions_added_later_appear(qtbot, window) -> None:
    sub = window.menus["tools"].addMenu("Sinov &menyusi")
    added = sub.addAction("Yangi && maxsus buyruq")
    calls = []
    added.triggered.connect(lambda: calls.append(1))
    hidden = sub.addAction("Ko'rinmas")
    hidden.setVisible(False)
    pal = window.show_palette()
    tools = t("gui.main.menu.tools").replace("&", "")
    found = [e for e in pal.all_entries() if e.text == "Yangi & maxsus buyruq"]
    assert len(found) == 1 and found[0].path == P.PATH_SEP.join([tools, "Sinov menyusi"])
    assert found[0].id.startswith("menu:")
    assert not [e for e in pal.all_entries() if e.text == "Ko'rinmas"]
    pal.set_query("maxsus")
    assert pal.current_entry() is found[0]
    assert pal.run_current()
    qtbot.waitUntil(lambda: calls == [1], timeout=TIMEOUT)


def test_disabled_entries_show_their_reason_and_never_run(qtbot, window) -> None:
    pal = window.show_palette()
    save = entry(pal, "act:save")
    export = entry(pal, "act:export")
    assert not save.enabled and save.reason             # the edit commands give their reason as tooltip
    assert not export.enabled and export.reason == t("gui.palette.reason.no_file")
    assert entry(pal, "act:open").enabled
    triggered = []
    window.action("export").triggered.connect(lambda: triggered.append("export"))
    pal.set_query("eksport")
    assert pal.current_entry() is export
    qtbot.keyClick(pal.edit, Qt.Key.Key_Return)
    assert pal.is_open()
    assert export.reason in pal.status.text()
    assert not pal.run_entry(export)
    index = pal.model.index(0, 0)
    qtbot.mouseClick(pal.list.viewport(), Qt.MouseButton.LeftButton, pos=pal.list.visualRect(index).center())
    qtbot.wait(20)
    assert triggered == [] and pal.is_open()
    # the tooltip and the painted second line carry the reason
    assert export.reason in pal.model.data(index, Qt.ItemDataRole.ToolTipRole)
    pal.close_palette()


def test_enter_runs_after_closing_with_the_focus_back(qtbot, app, data_file) -> None:
    w = new_window(qtbot, app)
    try:
        shown(qtbot, w)
        tab = open_ready(qtbot, w, data_file)
        view = tab.active_view()
        view.edit.setFocus()
        qtbot.waitUntil(lambda: QApplication.focusWidget() is view.edit, timeout=TIMEOUT)
        seen = []
        act = w.menus["tools"].addAction("Maxsus sinov buyrug'i")
        act.triggered.connect(lambda: seen.append((P.palette_of(w).is_open(), QApplication.focusWidget())))
        pal = w.show_palette()
        qtbot.waitUntil(lambda: QApplication.focusWidget() is pal.edit, timeout=TIMEOUT)
        qtbot.keyClicks(pal.edit, "maxsus sinov")
        assert pal.current_entry().text == "Maxsus sinov buyrug'i"
        qtbot.keyClick(pal.edit, Qt.Key.Key_Return)
        assert not pal.is_open() and seen == []          # it runs after the popup closed ...
        qtbot.waitUntil(lambda: bool(seen), timeout=TIMEOUT)
        assert seen == [(False, view.edit)]                # ... with the focus back where it was
        assert app.state.get(P.STATE_RECENT)[0] == "menu:" + "/".join(
            [t("gui.main.menu.tools").replace("&", ""), "Maxsus sinov buyrug'i"])
        # Esc closes without running anything; the focus goes back too
        pal = w.show_palette()
        qtbot.keyClicks(pal.edit, "maxsus")
        qtbot.keyClick(pal.edit, Qt.Key.Key_Escape)
        assert not pal.is_open()
        qtbot.wait(30)
        assert len(seen) == 1
        qtbot.waitUntil(lambda: QApplication.focusWidget() is view.edit, timeout=TIMEOUT)
    finally:
        w.close()


def test_palette_with_the_session_restore_bar_shown(qtbot, app, data_file) -> None:
    """While the "restore the last session?" bar is shown (the central widget is a holder: bar + document area)
    the palette sits over the document area under the bar, lists everything and runs with the focus back."""
    from PySide6.QtCore import QPoint

    from jsonreshape.gui.sessionstore import SessionState, TabState

    w = new_window(qtbot, app)
    try:
        shown(qtbot, w)
        tab = open_ready(qtbot, w, data_file)
        bar = w.workspaces.show_bar(SessionState(tabs=[TabState(path=str(data_file))], active=0))
        qtbot.waitUntil(lambda: bar.isVisible(), timeout=TIMEOUT)
        assert w.centralWidget() is not w.central
        view = tab.active_view()
        view.edit.setFocus()
        qtbot.waitUntil(lambda: QApplication.focusWidget() is view.edit, timeout=TIMEOUT)
        calls = []
        act = w.menus["tools"].addAction("Bar bilan sinov")
        act.triggered.connect(lambda: calls.append(QApplication.focusWidget()))
        pal = w.show_palette()
        area_top = w.central.mapTo(w, QPoint(0, 0)).y()
        bar_bottom = bar.mapTo(w, QPoint(0, bar.height())).y()
        assert pal.y() == area_top + 8 and pal.y() >= bar_bottom      # under the bar, over the document area
        assert pal.x() >= w.central.mapTo(w, QPoint(0, 0)).x()
        assert entry(pal, "act:copy_value").enabled and entry(pal, "act:copy_value").keys.startswith("Ctrl+C")
        qtbot.keyClicks(pal.edit, "bar bilan sinov")
        qtbot.keyClick(pal.edit, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: bool(calls), timeout=TIMEOUT)
        assert calls == [view.edit] and not pal.is_open()
        w.workspaces.hide_bar()
        assert w.centralWidget() is w.central
        pal = w.show_palette()
        assert pal.y() == w.central.mapTo(w, QPoint(0, 0)).y() + 8
        pal.close_palette()
    finally:
        w.close()


def test_ctrl_shift_p_opens_it_from_anywhere(qtbot, app, data_file) -> None:
    w = new_window(qtbot, app)
    try:
        shown(qtbot, w)
        action = w.action("palette")
        assert action.shortcut() == QKeySequence("Ctrl+Shift+P")
        assert action in w.menus["view"].actions() and w.menus["view"].actions()[0] is action
        tab = open_ready(qtbot, w, data_file)
        tab.active_view().edit.setFocus()
        qtbot.waitUntil(lambda: QApplication.focusWidget() is tab.active_view().edit, timeout=TIMEOUT)
        qtbot.keyClick(QApplication.focusWidget(), Qt.Key.Key_P,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        pal = P.palette_of(w)
        assert pal is not None and pal.is_open()
        qtbot.waitUntil(lambda: QApplication.focusWidget() is pal.edit, timeout=TIMEOUT)
        # pressed again it stays open with the entries collected anew and the query cleared
        qtbot.keyClicks(pal.edit, "abc")
        qtbot.keyClick(pal.edit, Qt.Key.Key_P,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        assert pal.is_open() and pal.query() == ""
        # a click outside closes it (the click is not swallowed)
        qtbot.mouseClick(w.status, Qt.MouseButton.LeftButton)
        assert not pal.is_open()
    finally:
        w.close()


def test_list_keys_move_the_current_row(qtbot, window) -> None:
    pal = window.show_palette()
    count = len(pal.entries())
    assert count > 2 * P.VISIBLE_ROWS and pal.current_row() == 0
    key = lambda k, mods=Qt.KeyboardModifier.NoModifier: qtbot.keyClick(pal.edit, k, mods)  # noqa: E731
    key(Qt.Key.Key_Down)
    assert pal.current_row() == 1
    key(Qt.Key.Key_PageDown)
    assert pal.current_row() == 1 + P.VISIBLE_ROWS - 1
    key(Qt.Key.Key_PageUp)
    assert pal.current_row() == 1
    key(Qt.Key.Key_End)
    assert pal.current_row() == count - 1
    key(Qt.Key.Key_Down)                     # wraps around
    assert pal.current_row() == 0
    key(Qt.Key.Key_Up)
    assert pal.current_row() == count - 1
    key(Qt.Key.Key_Home)
    assert pal.current_row() == 0
    qtbot.keyClicks(pal.edit, "ab")
    key(Qt.Key.Key_Home, Qt.KeyboardModifier.ShiftModifier)        # Shift+Home selects the typed text
    assert pal.edit.selectedText() == "ab"
    pal.set_query("zzqqxxjj")
    assert pal.entries() == [] and pal.empty_label.isVisibleTo(pal) and not pal.list.isVisibleTo(pal)
    key(Qt.Key.Key_Return)                   # nothing to run
    assert pal.is_open()
    pal.close_palette()


def test_mouse_click_runs_the_entry(qtbot, window) -> None:
    calls = []
    act = window.menus["help"].addAction("Bosib sinash")
    act.triggered.connect(lambda: calls.append(1))
    pal = window.show_palette()
    pal.set_query("bosib sinash")
    assert pal.entries()[0].action is act
    rect = pal.list.visualRect(pal.model.index(0, 0))
    qtbot.mouseClick(pal.list.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    assert not pal.is_open()
    qtbot.waitUntil(lambda: calls == [1], timeout=TIMEOUT)


def test_view_edit_commands_and_node_commands(qtbot, app, monkeypatch, data_file) -> None:
    install_stub_edit(monkeypatch)
    EditTreeStub.states = {"rename": (False, "Massiv elementining kaliti yo'q")}
    w = new_window(qtbot, app, view_registry=edit_registry())
    try:
        tab = open_ready(qtbot, w, data_file)
        qtbot.waitUntil(lambda: tab.session.edit is not None, timeout=TIMEOUT)
        view = tab.active_view()
        assert isinstance(view, EditTreeStub)
        tab.session.select(NodeRef(3, ("name",)))
        pal = w.show_palette()
        edit_value = entry(pal, "act:edit_edit_value")
        assert edit_value.enabled and edit_value.keys == "Enter"
        rename = entry(pal, "act:edit_rename")
        assert not rename.enabled and rename.reason == "Massiv elementining kaliti yo'q"
        # the node's context-menu commands (the shared node menu for the stub view)
        node = t("gui.palette.node")
        filt = entry(pal, "node:filter_by_value")
        assert filt.kind == "node" and filt.path == node and filt.enabled
        sort_asc = entry(pal, "node:sort_asc")
        assert sort_asc.path == P.PATH_SEP.join([node, t("gui.actions.sort")])
        for name in ("full_value", "show_in_tree", "open_new_tab", "stats", "compare_mark"):
            assert entry(pal, f"node:{name}").kind == "node", name
        # the commands of the Edit menu are not listed twice
        assert not [e for e in pal.all_entries() if e.id.startswith("node:edit_")]
        assert not [e for e in pal.all_entries() if e.id in ("node:copy_value_json", "node:copy_key")]
        # running a view edit command from the palette
        pal.set_query("qiymatni tahrir")
        assert pal.current_entry() is edit_value
        qtbot.keyClick(pal.edit, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: ("edit_value",) in view.calls, timeout=TIMEOUT)
        # a node command: "open in a new tab" opens the node's file again
        pal = w.show_palette()
        pal.set_query(t("gui.actions.open_new_tab"))
        new_tab = pal.current_entry()
        assert new_tab.id == "node:open_new_tab"
        assert pal.run_current()
        qtbot.waitUntil(lambda: w.tabs.count() == 2, timeout=TIMEOUT)
        wait_ready(qtbot, w.tab_widgets()[1].session)
    finally:
        EditTreeStub.states = {}
        w.close()


def test_the_real_tree_menu_commands(qtbot, app, data_file) -> None:
    """With the real views the tree's own context-menu items (expand operations) are listed too."""
    w = new_window(qtbot, app, view_registry=None)          # the real views (stub panels)
    try:
        tab = open_ready(qtbot, w, data_file)
        tree = tab.active_view()
        assert tree.view_id == "tree"
        qtbot.waitUntil(lambda: tree.grid.row_count() > 0, timeout=TIMEOUT)
        tree.select_row(0)

        def node_known() -> bool:          # the row's node info is loaded (a right-click menu is possible)
            menu = tree.context_menu(0)
            if menu is None:
                return False
            menu.deleteLater()
            return True

        qtbot.waitUntil(node_known, timeout=TIMEOUT)
        pal = w.show_palette()
        expand = entry(pal, "node:tree_expand_all")
        assert expand.keys == "*" and expand.path == t("gui.palette.node")
        assert entry(pal, "node:tree_expand_level_2").path.endswith(P.clean_text(t("gui.tree.menu.expand_level"))[0])
        pal.set_query(P.clean_text(t("gui.tree.menu.expand_all"))[0])
        assert any(e.id == "node:tree_expand_all" for e in pal.entries())
        pal.close_palette()
    finally:
        w.close()


def test_tab_and_recent_file_entries_run(qtbot, window, tmp_path) -> None:
    a = write_json(tmp_path / "a.json", records(3))
    b = write_json(tmp_path / "b.json", records(4))
    c = write_json(tmp_path / "c.json", records(5))
    tab_a = open_ready(qtbot, window, a)
    tab_b = open_ready(qtbot, window, b)
    window.state.add_recent(str(c))
    assert window.current_tab() is tab_b
    pal = window.show_palette()
    pal.set_query("tab a.json")
    assert pal.current_entry().kind == "tab"
    qtbot.keyClick(pal.edit, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: window.current_tab() is tab_a, timeout=TIMEOUT)
    pal = window.show_palette()
    pal.set_query("ochish c.json")
    assert pal.current_entry().id == "open:" + os.path.normcase(os.path.abspath(str(c)))
    qtbot.keyClick(pal.edit, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: window.tabs.count() == 3, timeout=TIMEOUT)
    wait_ready(qtbot, window.current_tab().session)
    assert window.current_tab().session.name == "c.json"
    # an open file chosen again is focused, not opened twice
    pal = window.show_palette()
    pal.set_query("ochish a.json")
    pal.run_current()
    qtbot.waitUntil(lambda: window.current_tab() is tab_a, timeout=TIMEOUT)
    assert window.tabs.count() == 3


def test_recent_commands_persist_and_come_first(qtbot, app, tmp_path) -> None:
    w = new_window(qtbot, app)
    try:
        for ident in ("act:dock_search", "act:dock_tasks"):        # dock toggles: enabled without a file
            pal = w.show_palette()
            assert pal.run_entry(entry(pal, ident))
            qtbot.wait(10)
        assert w.docks["tasks"].isVisibleTo(w) and w.docks["search"].isVisibleTo(w)
        assert app.state.get(P.STATE_RECENT) == ["act:dock_tasks", "act:dock_search"]
        pal = w.show_palette()
        first = pal.ranked()[:2]
        assert [r.entry.id for r in first] == ["act:dock_tasks", "act:dock_search"] and all(r.recent for r in first)
        assert not pal.ranked()[2].recent
        pal.close_palette()
        app.tasks.wait_idle(10)
        assert app.state.save()
    finally:
        w.close()
    # a new window (state read from the file) lists them first too
    ctx2 = make_app(tmp_path)
    try:
        assert ctx2.state.get(P.STATE_RECENT) == ["act:dock_tasks", "act:dock_search"]
        w2 = new_window(qtbot, ctx2)
        pal = w2.show_palette()
        assert ids(pal)[:2] == ["act:dock_tasks", "act:dock_search"]
        pal.close_palette()
        w2.close()
    finally:
        ctx2.tasks.cancel_all()
        assert ctx2.tasks.shutdown(30)


def test_broken_recent_value_is_ignored(qtbot, app) -> None:
    app.state.set(P.STATE_RECENT, {"buzuq": True})
    w = new_window(qtbot, app)
    try:
        pal = w.show_palette()
        assert pal.entries() and not any(r.recent for r in pal.ranked())
        assert pal.run_entry(entry(pal, "act:dock_tasks"))
        assert app.state.get(P.STATE_RECENT) == ["act:dock_tasks"]
    finally:
        w.close()


def test_palette_shows_the_current_keys(qtbot, window) -> None:
    assert window.keymap.set_keys("export", ["Ctrl+Alt+E"]) is None
    pal = window.show_palette()
    assert entry(pal, "act:export").keys == "Ctrl+Alt+E"
    assert window.keymap.set_keys("export", []) is None
    pal = window.show_palette()
    assert entry(pal, "act:export").keys == ""
    window.keymap.reset_all()
    pal = window.show_palette()
    assert entry(pal, "act:export").keys == "Ctrl+E"
    pal.close_palette()


def test_english_texts(qtbot, app, lang_en) -> None:
    w = new_window(qtbot, app)
    try:
        pal = w.show_palette()
        pal.set_query("save")
        top = [e.text for e in pal.entries()[:3]]
        assert "Save" in top and all(text.lower().startswith("save") for text in top), top
        assert entry(pal, "act:save").reason          # no file: disabled with its reason
        pal.set_query("palette")
        assert pal.entries()[0].id == "act:palette" and pal.entries()[0].text == "Command palette…"
        assert pal.edit.placeholderText() == "Type a command name: save, export, filter…"
        pal.close_palette()
    finally:
        w.close()


def test_theme_aware(qtbot, window, qapp) -> None:
    palette_before = QPalette(qapp.palette())
    style = qapp.style().name()
    try:
        pal = window.show_palette()
        theme.apply_theme(qapp, "dark")
        assert theme.color("panel").name() in pal.styleSheet()
        dark = theme.color("panel").name()
        theme.apply_theme(qapp, "light")
        assert theme.color("panel").name() in pal.styleSheet() and theme.color("panel").name() != dark
        pal.list.viewport().grab()              # paints the rows (delegate) without errors
    finally:
        theme._reset_for_tests()
        qapp.setStyle(style)
        qapp.setPalette(palette_before)
        theme.invalidate()


def test_open_is_fast_with_hundreds_of_entries(qtbot, window, tmp_path) -> None:
    extra = window.menus["tools"].addMenu("Ko'p buyruqlar")
    for i in range(200):
        extra.addAction(f"Sinov buyrug'i {i}")
    for i in range(20):
        window.state.add_recent(str(tmp_path / f"fayl_{i}.json"))
    pal = window.show_palette()                     # cold: modules and fold caches
    assert len(pal.all_entries()) >= 300
    times = []
    for _ in range(5):
        pal.close_palette()
        pal = window.show_palette()
        times.append(pal.open_ms)
    assert min(times) < 50, times
    start = time.perf_counter()
    for text in ("s", "si", "sin", "sinov", "sinov 1", "sinov 19"):
        pal.set_query(text)
    per_key = (time.perf_counter() - start) / 6 * 1000
    assert per_key < 50, per_key
    assert pal.entries()[0].text == "Sinov buyrug'i 19"
    pal.close_palette()
