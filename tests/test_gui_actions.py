"""jsonreshape.gui.actions: the node context menu (structure, enabled states with
tooltips, every item calling the session), popup lifetime, Edit-menu copy actions."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QWidget  # noqa: E402

from jsonreshape.errors import UserInputError  # noqa: E402
from jsonreshape.gui import actions  # noqa: E402
from jsonreshape.gui.actions import add_copy_actions, node_menu, popup_node_menu  # noqa: E402
from jsonreshape.gui.copyfmt import COPY_FORMATS, PATH_STYLES  # noqa: E402
from jsonreshape.gui.widgets import FullValueDialog, ViewBase  # noqa: E402

from test_gui_widgets import FakeSession, Ref, open_doc  # noqa: E402

pytestmark = pytest.mark.gui


@pytest.fixture
def app(qapp, tmp_path):
    from jsonreshape.gui.context import AppContext
    from jsonreshape.gui.guistate import GuiState

    ctx = AppContext(state=GuiState(tmp_path / "gui_state.json"), headless=True)
    yield ctx
    ctx.tasks.shutdown(5)


@pytest.fixture
def reported(app):
    errors: list[BaseException] = []
    app.error_reported.connect(errors.append)
    return errors


@pytest.fixture
def session(app, tmp_path):
    doc = open_doc(tmp_path)
    yield FakeSession(app, doc, doc.source_path)
    doc.close()


@pytest.fixture
def host(qtbot):
    w = QWidget()
    qtbot.addWidget(w)
    return w


class TreeLike(ViewBase):
    view_id = "tree"


class TableLike(ViewBase):
    view_id = "table"


def act(menu: QMenu, name: str) -> QAction:
    found = menu.findChild(QAction, name)
    assert found is not None, name
    return found


def clipboard() -> str:
    return QGuiApplication.clipboard().text()


def wait_tasks(app) -> None:
    assert app.tasks.wait_idle(10)


# ----------------------------------------------------------------------------------
# structure and states
# ----------------------------------------------------------------------------------
def test_menu_structure(host, session, lang_uz) -> None:
    menu = node_menu(host, session, Ref(0, ("addr", "city")))
    top = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert top == ["Nusxalash", "Shu qiymat bo'yicha filtr", "Shu maydon bo'yicha tartiblash",
                   "Taqqoslash uchun belgilash", "Belgilangan bilan taqqoslash", "Statistika",
                   "Shu tugunni qismlarga bo'lish…", "To'liq ko'rish", "Daraxtda ko'rsatish", "Yangi tabda ochish"]
    # stage 5 (tests/test_gui_roundb_shell.py): nothing marked yet; a host without a statistics dock
    assert not act(menu, "compare_with_marked").isEnabled() and not act(menu, "stats").isEnabled()
    for name in ("compare_with_marked", "stats"):
        act(menu, name).setObjectName("")
    copy_menu = menu.findChild(QMenu, "copy_menu")
    names = [a.objectName() for a in copy_menu.actions() if not a.isSeparator()]
    assert names == [f"copy_value_{f}" for f in COPY_FORMATS] + [f"copy_path_{s}" for s in PATH_STYLES] + ["copy_key"]
    assert act(menu, "copy_value_json").text() == "Qiymat: JSON (chiroyli)"
    assert act(menu, "copy_path_dotted").text() == "Yo'l (Nuqtali): [0].addr.city"
    assert act(menu, "copy_path_pointer").text() == "Yo'l (JSON Pointer): /0/addr/city"
    assert act(menu, "copy_key").text() == "Kalit: city"
    sort_menu = menu.findChild(QMenu, "sort_menu")
    assert [a.objectName() for a in sort_menu.actions()] == ["sort_asc", "sort_desc"]
    assert all(a.isEnabled() for a in menu.findChildren(QAction) if a.objectName())
    assert menu.toolTipsVisible() and copy_menu.toolTipsVisible()


def test_shortcuts_are_shown_not_registered(host, session) -> None:
    menu = node_menu(host, session, Ref(0, ("id",)))
    value = act(menu, "copy_value_json")
    path = act(menu, "copy_path_dotted")
    assert value.shortcut() == QKeySequence(QKeySequence.StandardKey.Copy)
    assert path.shortcut() == QKeySequence("Ctrl+Shift+C")
    assert value.isShortcutVisibleInContextMenu()
    from PySide6.QtCore import Qt

    assert value.shortcutContext() == Qt.ShortcutContext.WidgetShortcut


def test_root_ref_disables_what_does_not_apply(host, session, lang_uz) -> None:
    menu = node_menu(host, session, Ref(None, ()))
    key = act(menu, "copy_key")
    assert not key.isEnabled() and key.toolTip() == "Ildiz tugunning kaliti yo'q."
    assert key.text() == "Kalit: (ildiz)"
    assert act(menu, "copy_path_dotted").text() == "Yo'l (Nuqtali): $"
    assert act(menu, "copy_path_python").text() == "Yo'l (Python): (ildiz)"
    filt = act(menu, "filter_by_value")
    assert not filt.isEnabled() and "yozuvlar ichidagi" in filt.toolTip()
    for name in ("sort_asc", "sort_desc"):
        assert not act(menu, name).isEnabled()
    assert not menu.findChild(QMenu, "sort_menu").isEnabled()
    # always available
    for name in ("copy_value_json", "copy_path_jsonpath", "full_value", "show_in_tree", "open_new_tab"):
        assert act(menu, name).isEnabled(), name


def test_whole_record_can_be_filtered_but_not_sorted(host, session, lang_uz) -> None:
    menu = node_menu(host, session, Ref(1, ()))
    assert act(menu, "filter_by_value").isEnabled()
    assert not act(menu, "sort_asc").isEnabled()
    assert "yozuvning o'zi uchun emas" in act(menu, "sort_desc").toolTip()
    assert act(menu, "copy_key").text() == "Kalit: 1"


def test_big_container_disables_filter_when_known(host, session, lang_uz) -> None:
    class Info:
        kind = "array"
        size = actions.FILTER_MAX_BYTES + 1

    menu = node_menu(host, session, Ref(0, ("tags",)), node=Info())
    filt = act(menu, "filter_by_value")
    assert not filt.isEnabled() and "juda katta" in filt.toolTip()
    Info.kind = "string"
    assert act(node_menu(host, session, Ref(0, ("tags",)), node=Info()), "filter_by_value").isEnabled()


def test_tree_view_disables_show_in_tree(host, session, lang_uz) -> None:
    tree = TreeLike(session)
    menu = node_menu(host, session, Ref(0, ("id",)), view=tree)
    item = act(menu, "show_in_tree")
    assert not item.isEnabled() and item.toolTip()
    assert act(node_menu(host, session, Ref(0, ("id",)), view=TableLike(session)), "show_in_tree").isEnabled()


def test_ampersand_in_keys_is_literal(host, session, lang_en) -> None:
    menu = node_menu(host, session, Ref(0, ("a&b",)))
    assert act(menu, "copy_key").text() == "Key: a&&b"   # "&&" shows one literal "&"
    assert "a&&b" in act(menu, "copy_path_dotted").text()


def test_long_paths_are_elided(host, session) -> None:
    class S(FakeSession):
        def absolute_path(self, ref):
            return tuple(f"segment{i}" for i in range(40))

    s = S(session.app, session.doc, session.path)
    menu = node_menu(host, s, Ref(0, ("x",)))
    text = act(menu, "copy_path_dotted").text()
    assert "…" in text and len(text) < 120


def test_absolute_path_failure_disables_path_items(host, session, lang_uz) -> None:
    class S(FakeSession):
        def absolute_path(self, ref):
            raise RuntimeError("no doc")

    s = S(session.app, session.doc, session.path)
    menu = node_menu(host, s, Ref(0, ("x",)))
    for style in PATH_STYLES:
        assert not act(menu, f"copy_path_{style}").isEnabled()
    assert not act(menu, "copy_key").isEnabled()
    assert act(menu, "copy_value_json").isEnabled()


# ----------------------------------------------------------------------------------
# triggering
# ----------------------------------------------------------------------------------
def test_copy_items(host, session) -> None:
    ref = Ref(0, ("tags",))
    menu = node_menu(host, session, ref)
    act(menu, "copy_value_json").trigger()
    wait_tasks(session.app)
    assert clipboard() == '[\n  "a",\n  "b"\n]'
    act(menu, "copy_value_python").trigger()
    wait_tasks(session.app)
    assert clipboard() == "[\n  'a',\n  'b'\n]"
    act(menu, "copy_value_csv").trigger()
    wait_tasks(session.app)
    assert clipboard() == "a;b"
    act(menu, "copy_value_csv_header").trigger()
    wait_tasks(session.app)
    assert clipboard() == "$\na;b"
    act(menu, "copy_value_json_compact").trigger()
    wait_tasks(session.app)
    assert clipboard() == '["a","b"]'
    expected = {"dotted": "[0].tags", "jsonpath": "$[0].tags", "pointer": "/0/tags", "python": '[0]["tags"]',
                "js": "[0].tags"}
    for style, text in expected.items():
        act(menu, f"copy_path_{style}").trigger()
        assert clipboard() == text
    act(menu, "copy_key").trigger()
    assert clipboard() == "tags"


def test_copy_tasks_are_owned_by_the_view(host, session) -> None:
    view = TableLike(session)
    menu = node_menu(host, session, Ref(0, ()), view=view)
    act(menu, "copy_value_json").trigger()
    tasks = session.app.tasks.tasks(include_silent=True)
    assert tasks and all(t.owner is view for t in tasks)
    view.shutdown()            # detached: nothing reaches the clipboard
    QGuiApplication.clipboard().setText("kept")
    wait_tasks(session.app)
    assert clipboard() == "kept"


def test_filter_by_value(host, session) -> None:
    ref = Ref(0, ("addr", "city"))
    act(node_menu(host, session, ref), "filter_by_value").trigger()
    wait_tasks(session.app)
    assert session.calls == [("filter", ref, "Toshkent")]
    session.calls.clear()
    small = Ref(0, ("addr",))
    act(node_menu(host, session, small), "filter_by_value").trigger()
    wait_tasks(session.app)
    assert session.calls == [("filter", small, {"city": "Toshkent", "zip": None})]


def test_filter_exact_number_value(host, session) -> None:
    ref = Ref(0, ("n",))
    act(node_menu(host, session, ref), "filter_by_value").trigger()
    wait_tasks(session.app)
    (_, _, value), = session.calls
    assert repr(value) == "1.10"


def test_filter_refuses_big_containers_in_the_task(host, app, reported, tmp_path, lang_uz) -> None:
    import json

    doc = open_doc(tmp_path, json.dumps([{"big": list(range(3000)), "ok": 1}]))
    try:
        session = FakeSession(app, doc, doc.source_path)
        act(node_menu(host, session, Ref(0, ("big",))), "filter_by_value").trigger()
        wait_tasks(app)
        assert session.calls == []
        assert len(reported) == 1
        exc = reported[0]
        assert isinstance(exc, UserInputError) and exc.key == "gui.actions.filter_too_large"
        assert exc.hint
    finally:
        doc.close()


def test_filter_too_large_node_reported(host, app, reported, tmp_path, lang_uz) -> None:
    doc = open_doc(tmp_path, '[{"s": "' + "x" * 500 + '"}]', large=100)
    try:
        session = FakeSession(app, doc, doc.source_path)
        act(node_menu(host, session, Ref(0, ("s",))), "filter_by_value").trigger()
        wait_tasks(app)
        assert session.calls == []
        assert [getattr(e, "key", None) for e in reported] == ["gui.actions.filter_too_large"]
    finally:
        doc.close()


def test_sort_items(host, session) -> None:
    ref = Ref(0, ("addr", "city"))
    menu = node_menu(host, session, ref)
    act(menu, "sort_asc").trigger()
    act(menu, "sort_desc").trigger()
    assert session.calls == [("sort", ("addr", "city"), False), ("sort", ("addr", "city"), True)]


def test_show_in_tree(host, session) -> None:
    view = TableLike(session)
    ref = Ref(1, ("name",))
    act(node_menu(host, session, ref, view=view), "show_in_tree").trigger()
    assert session.calls == [("select", ref, view, True)]


def test_open_in_new_tab(qtbot, host, session) -> None:
    ref = Ref(0, ("addr",))
    session.record_path = "$"
    with qtbot.waitSignal(session.app.open_requested, timeout=1000) as blocker:
        act(node_menu(host, session, ref), "open_new_tab").trigger()
    path, options = blocker.args
    assert path == str(session.path)
    assert options == {"ref": ref, "record_path": "$", "format": "array"}


def test_open_in_new_tab_without_request_open(qtbot, host, session) -> None:
    class BareApp:
        def __init__(self, real):
            self.open_requested = real.open_requested
            self.tasks = real.tasks

    s = FakeSession(BareApp(session.app), session.doc, session.path)
    s.format = None
    with qtbot.waitSignal(session.app.open_requested, timeout=1000) as blocker:
        act(node_menu(host, s, Ref(0, ())), "open_new_tab").trigger()
    assert blocker.args[1] == {"ref": Ref(0, ())}


def test_full_value_item_opens_the_dialog(qtbot, host, session) -> None:
    act(node_menu(host, session, Ref(0, ("name",))), "full_value").trigger()
    dialogs = host.findChildren(FullValueDialog)
    assert len(dialogs) == 1
    dialog = dialogs[0]
    qtbot.waitUntil(dialog.is_loaded, timeout=10000)
    assert dialog.text() == "Ali"
    assert dialog.isVisible()
    dialog.close()


def test_full_value_without_parent_survives(qtbot, session) -> None:
    import gc

    dialog = actions.show_full_value(None, session, Ref(1, ("name",)))
    del dialog
    gc.collect()
    found = [w for w in QApplication.topLevelWidgets() if isinstance(w, FullValueDialog) and w.isVisible()]
    assert found
    qtbot.waitUntil(found[-1].is_loaded, timeout=10000)
    assert found[-1].text() == "Vali"
    for w in found:
        w.close()


# ----------------------------------------------------------------------------------
# extra items, popup, Edit menu
# ----------------------------------------------------------------------------------
def test_extra_items(host, session, lang_uz) -> None:
    hits = []
    own = QAction("Hammasini ochish", host)
    own.setObjectName("expand_all")
    sub = QMenu("Darajagacha ochish", host)
    sub.addAction("1")
    extra = [own, None, ("Yopish", lambda: hits.append("collapse")),
             ("Mumkin emas", lambda: hits.append("no"), False, "Sababi bor"),
             ("Maslahat", lambda: hits.append("tip"), True, "Shunchaki maslahat"), sub]
    menu = node_menu(host, session, Ref(0, ("addr",)), extra=extra)
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts == ["Nusxalash", "Shu qiymat bo'yicha filtr", "Shu maydon bo'yicha tartiblash",
                     "Hammasini ochish", "Yopish", "Mumkin emas", "Maslahat", "Darajagacha ochish",
                     "Taqqoslash uchun belgilash", "Belgilangan bilan taqqoslash", "Statistika",
                     "Shu tugunni qismlarga bo'lish…", "To'liq ko'rish", "Daraxtda ko'rsatish", "Yangi tabda ochish"]
    by_text = {a.text(): a for a in menu.actions()}
    by_text["Yopish"].trigger()
    assert hits == ["collapse"]
    disabled = by_text["Mumkin emas"]
    assert not disabled.isEnabled() and disabled.toolTip() == "Sababi bor"
    assert by_text["Maslahat"].isEnabled() and by_text["Maslahat"].toolTip() == "Shunchaki maslahat"
    assert by_text["Darajagacha ochish"].menu() is sub


def test_popup_menu_deletes_itself(qtbot, host, session) -> None:
    host.show()
    menu = popup_node_menu(host, session, Ref(0, ("id",)), host.mapToGlobal(QPoint(5, 5)))
    qtbot.waitUntil(menu.isVisible, timeout=2000)
    gone = []
    menu.destroyed.connect(lambda *_args: gone.append(1))
    menu.hide()
    qtbot.waitUntil(lambda: bool(gone), timeout=2000)


def test_viewbase_show_node_menu(qtbot, session) -> None:
    view = TableLike(session)
    qtbot.addWidget(view)
    view.show()
    menu = view.show_node_menu(Ref(0, ("id",)), view.mapToGlobal(QPoint(2, 2)), extra=[("X", lambda: None)])
    qtbot.waitUntil(menu.isVisible, timeout=2000)
    assert any(a.text() == "X" for a in menu.actions())
    act(menu, "show_in_tree").trigger()
    assert session.calls == [("select", Ref(0, ("id",)), view, True)]
    menu.hide()


def test_add_copy_actions(host, lang_uz) -> None:
    calls = []

    class View:
        def copy_value(self, fmt):
            calls.append(("value", fmt))

        def copy_path(self, style):
            calls.append(("path", style))

        def copy_key(self):
            calls.append(("key",))

    current = [View()]
    menu = QMenu(host)
    acts = add_copy_actions(menu, lambda: current[0], shortcuts=True)
    assert set(acts) == {f"value_{f}" for f in COPY_FORMATS} | {f"path_{s}" for s in PATH_STYLES} | {"key"}
    assert acts["value_json"].shortcut() == QKeySequence(QKeySequence.StandardKey.Copy)
    assert acts["path_dotted"].shortcut() == QKeySequence("Ctrl+Shift+C")
    assert acts["path_js"].text() == "Yo'l: JavaScript"
    acts["value_python"].trigger()
    acts["path_pointer"].trigger()
    acts["key"].trigger()
    assert calls == [("value", "python"), ("path", "pointer"), ("key",)]
    current[0] = None
    acts["value_json"].trigger()     # no active view: nothing happens
    assert len(calls) == 3
    plain = add_copy_actions(QMenu(host), lambda: None)
    assert plain["value_json"].shortcut().isEmpty()
