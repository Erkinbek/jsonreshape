"""DocumentTab / ViewPane (jsonreshape/gui/doctab.py; ARCHITECTURE §11.8): lazy views (one
instance per kind per pane, activate() when shown), the active pane (focus tracking),
second pane / orientation, view wiring (text → raw, schema → table column / filter,
reveal → tree), the info bars (JSON error + go to error, JSONL broken lines list, format
warnings, record path choice + reopen, file changed / grown + reopen, open failure +
retry, cancelled open), the busy title and shutdown (sessions closed, nothing after)."""

from __future__ import annotations

import json
import os

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from guishell_helpers import (
    TIMEOUT,
    RawStub,
    StubView,
    TableStub,
    TreeStub,
    jsonl_with_broken,
    make_app,
    records,
    stub_registry,
    wait_ready,
    wait_state,
    wrapped_file,
    write_json,
)
from jsonreshape.gui.doctab import BUSY_MARK, DocumentTab, ViewPane, default_view_registry, view_label
from jsonreshape.gui.session import DocSession, NodeRef

pytestmark = pytest.mark.gui


@pytest.fixture
def app(qapp, tmp_path, lang_uz):
    ctx = make_app(tmp_path)
    yield ctx
    ctx.tasks.cancel_all()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def make_tab(qtbot, app):
    tabs: list[DocumentTab] = []

    def factory(path, *, start=True, wait=True, registry=None, **kw) -> DocumentTab:
        kw.setdefault("use_cache", False)
        session = DocSession(app, path, **kw)
        tab = DocumentTab(session, view_registry=registry if registry is not None else stub_registry())
        qtbot.addWidget(tab)
        tabs.append(tab)
        if start:
            session.start()
            if wait:
                wait_ready(qtbot, session)
        return tab

    yield factory
    for tab in tabs:
        tab.shutdown()
    assert app.tasks.wait_idle(30)


def click_bar_button(bar, text: str) -> None:
    for button in bar.buttons():
        if button.text() == text:
            button.click()
            return
    raise AssertionError(f"no button {text!r} in {[b.text() for b in bar.buttons()]}")


# ----------------------------------------------------------------------------------------
# panes and views
# ----------------------------------------------------------------------------------------
def test_default_panes_and_lazy_views(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(10)))
    p0, p1 = tab.panes
    assert p0.current_kind() == "tree" and p1.current_kind() == "text"
    assert isinstance(p0.current_view(), TreeStub) and ("activate",) in p0.current_view().calls
    assert set(p0.views()) == {"tree"} and set(p1.views()) == {"text"}          # nothing else created
    assert tab.active_pane() is p0 and tab.active_view() is p0.current_view()
    tree = p0.current_view()
    table = tab.set_active_view_kind("table")
    assert isinstance(table, TableStub) and p0.current_kind() == "table"
    assert tab.set_active_view_kind("tree") is tree                           # one instance per kind
    assert tree.names().count("activate") == 2
    assert tab.available_kinds() == ["tree", "table", "text", "raw", "schema"]
    assert p0.view("raw") is None
    labels = [p0.selector.tabText(i) for i in range(p0.selector.count())]
    assert labels == [view_label(k) for k in ("tree", "table", "text", "raw", "schema")]
    assert p0.selector_kinds() == ["tree", "table", "text", "raw", "schema"]
    assert p0.selector.tabToolTip(1) == "Jadval (Ctrl+2)"
    assert p0.selector.currentIndex() == 0 and p1.selector.currentIndex() == 2   # follow set_view
    assert view_label("tree") == "Daraxt" and view_label("schema") == "Tuzilma"


def test_selector_click_and_active_pane(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(10)))
    changes = []
    tab.active_view_changed.connect(lambda: changes.append(tab.active_kind()))
    pane = tab.panes[1]
    rect = pane.selector.tabRect(pane.selector_kinds().index("raw"))
    qtbot.mouseClick(pane.selector, Qt.MouseButton.LeftButton, pos=rect.center())
    assert tab.active_pane_index() == 1 and tab.active_kind() == "raw"
    assert isinstance(tab.active_view(), RawStub)
    assert changes and changes[-1] == "raw"
    assert tab.panes[1].is_active() and not tab.panes[0].is_active()
    assert pane.selector_color("raw").isValid()               # accent: the active pane's view
    assert not tab.panes[0].selector_color("tree").isValid()
    # a click on the current view of the other pane makes that pane active
    first = tab.panes[0]
    qtbot.mouseClick(first.selector, Qt.MouseButton.LeftButton, pos=first.selector.tabRect(0).center())
    assert tab.active_pane_index() == 0 and tab.active_kind() == "tree"


def test_focus_tracking_sets_the_active_pane(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(10)))
    tab.show()
    tab.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is tab, timeout=TIMEOUT)
    text_view = tab.panes[1].current_view()
    text_view.edit.setFocus()
    qtbot.waitUntil(lambda: tab.active_pane_index() == 1, timeout=TIMEOUT)
    assert tab.active_view() is text_view
    tab.panes[0].current_view().edit.setFocus()
    qtbot.waitUntil(lambda: tab.active_pane_index() == 0, timeout=TIMEOUT)


def test_second_pane_and_orientation(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(10)))
    tab.set_active_pane(1)
    tab.set_second_pane_visible(False)
    assert not tab.second_pane_visible() and tab.active_pane_index() == 0
    tab.set_active_pane(1)                                     # hidden: stays on the first
    assert tab.active_pane_index() == 0
    tab.set_second_pane_visible(True)
    assert tab.second_pane_visible() and tab.panes[1].current_kind() == "text"
    tab.set_orientation(Qt.Orientation.Vertical)
    assert tab.orientation() == Qt.Orientation.Vertical


def test_missing_view_kinds_are_left_out(qtbot, make_tab, tmp_path) -> None:
    registry = {"table": TableStub, "raw": RawStub}
    tab = make_tab(write_json(tmp_path / "a.json", records(5)), registry=registry)
    assert tab.available_kinds() == ["table", "raw"]
    assert tab.panes[0].current_kind() == "table" and tab.panes[1].current_kind() == "raw"
    assert tab.set_active_view_kind("tree") is None


def test_view_construction_failure_is_reported(qtbot, app, make_tab, tmp_path) -> None:
    class Broken(StubView):
        def __init__(self, session, parent=None):
            raise RuntimeError("view bug")

    tab = make_tab(write_json(tmp_path / "a.json", records(5)), registry={"tree": TreeStub, "schema": Broken})
    assert tab.set_active_view_kind("schema") is None
    assert any(isinstance(e, RuntimeError) for e in app.errors)
    assert tab.active_kind() == "tree"


def test_default_view_registry_skips_missing_modules(monkeypatch) -> None:
    import jsonreshape.gui.doctab as doctab

    monkeypatch.setattr(doctab, "VIEW_MODULES", {**doctab.VIEW_MODULES, "tree": ("jsonreshape.gui.no_such_view", "X")})
    registry = default_view_registry()
    assert "tree" not in registry


# ----------------------------------------------------------------------------------------
# view wiring
# ----------------------------------------------------------------------------------------
def test_text_open_raw_goes_to_the_other_pane(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(10)))
    text_view = tab.panes[1].current_view()
    text_view.open_raw_requested.emit(123, 45)
    raw = tab.panes[0].current_view()
    assert isinstance(raw, RawStub) and ("goto_offset", 123, 45) in raw.calls


def test_schema_add_column_and_filter_field(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(10)))
    schema = tab.set_active_view_kind("schema")             # pane 0
    schema.add_column_requested.emit("contact.zip")
    table = tab.panes[1].current_view()                      # created in the other pane
    assert isinstance(table, TableStub) and ("add_column", "contact.zip") in table.calls
    schema.add_column_requested.emit("name")                 # the existing table is reused
    assert tab.views("table") == [table] and ("add_column", "name") in table.calls
    with qtbot.waitSignal(tab.filter_field_requested, timeout=TIMEOUT) as blocker:
        schema.filter_field_requested.emit("tags")
    assert blocker.args == ["tags"]


def test_reveal_switches_the_first_pane_to_the_tree(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(10)))
    tab.panes[0].set_view("table")
    tab.set_active_pane(0)
    tab.session.select(NodeRef(2, ()), reveal=True)
    # the pane NOT holding the active view shows the tree (review finding C11: the user's
    # table in the active pane used to be replaced)
    assert tab.panes[0].current_kind() == "table" and tab.panes[1].current_kind() == "tree"
    tab.panes[1].set_view("text")
    tab.set_active_pane(1)
    tab.session.select(NodeRef(1, ()), reveal=True)
    assert tab.panes[0].current_kind() == "tree" and tab.panes[1].current_kind() == "text"
    tab.panes[0].set_view("table")
    tab.panes[1].set_view("tree")                            # a visible pane shows the tree: unchanged
    tab.session.select(NodeRef(3, ()), reveal=True)
    assert tab.panes[0].current_kind() == "table"


# ----------------------------------------------------------------------------------------
# info bars
# ----------------------------------------------------------------------------------------
def test_json_error_bar_and_go_to_error(qtbot, make_tab, tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('[{"a": 1},\n {"a": 2},\n {"a": 3}, {"a": tru}]', encoding="utf-8")
    tab = make_tab(path)
    doc = tab.session.doc
    assert doc.error is not None
    bar = tab.info_bar("json_error")
    assert bar is not None and bar.kind() == "error"
    assert "3-qator" in bar.message() and f"{doc.error.offset}-bayt" in bar.message()
    assert "3 ta yozuv ochildi" in bar.detail_label.text()
    click_bar_button(bar, "Xatoga o'tish")
    raw = tab.panes[1].current_view()
    assert isinstance(raw, RawStub)
    assert ("goto_offset", doc.error_data_offset, 1) in raw.calls
    # second pane hidden: the active pane is used
    tab.set_second_pane_visible(False)
    tab.go_to_error()
    assert isinstance(tab.panes[0].current_view(), RawStub)


def test_jsonl_broken_lines_bar_and_dialog(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(jsonl_with_broken(tmp_path / "b.jsonl", 30, (2, 9)))
    bar = tab.info_bar("broken")
    assert bar is not None and bar.message() == "2 ta buzuq qator o'tkazib yuborildi."
    with qtbot.waitSignal(tab.broken_lines_dialog_opened, timeout=TIMEOUT) as blocker:
        click_bar_button(bar, "Ro'yxat")
    dialog = blocker.args[0]
    assert dialog.row_count() == 2
    dialog.go(1)
    raw = tab.panes[1].current_view()
    assert isinstance(raw, RawStub)
    bl = dialog.line_at(1)
    assert ("goto_offset", bl.offset, bl.end - bl.offset) in raw.calls
    dialog.close()


def test_warnings_bar_and_dismissal(qtbot, make_tab, tmp_path) -> None:
    path = tmp_path / "brace.json"
    path.write_text('{ {"a": 1}, {"a": 2} }', encoding="utf-8")
    tab = make_tab(path)
    assert tab.session.doc.kind == "brace_list"
    bar = tab.info_bar("warnings")
    assert bar is not None and bar.message() == tab.session.warning_texts()[0]
    bar.close_bar()
    assert tab.info_bar("warnings") is None
    tab._refresh_warnings()                                   # same warnings: stays dismissed
    assert tab.info_bar("warnings") is None


def test_record_path_bar_and_reopen(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(wrapped_file(tmp_path / "w.json", 50))
    bar = tab.info_bar("record_path")
    assert bar is not None and bar.message() == "Yozuvlar yo'li: data (avtomatik)"
    assert "other" in bar.detail_label.text()
    with qtbot.waitSignal(tab.record_path_dialog_opened, timeout=TIMEOUT) as blocker:
        click_bar_button(bar, "O'zgartirish…")
    dialog = blocker.args[0]
    texts = [dialog.list.topLevelItem(i).text(0) for i in range(dialog.list.topLevelItemCount())]
    dialog.choose_row(texts.index("other"))
    with qtbot.waitSignal(tab.session.index_finished, timeout=TIMEOUT):
        dialog.accept()
    assert tab.session.doc.info.record_path == ("other",)
    bar = tab.info_bar("record_path")
    assert bar is not None and bar.message() == "Yozuvlar yo'li: other (tanlangan)"


def test_plain_array_has_no_bars(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(10)))
    assert tab.info_bar_keys() == []
    assert not tab.bar_box.isVisibleTo(tab)


def test_file_changed_and_grown(qtbot, make_tab, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(DocSession, "CHANGE_POLL_MS", 50)
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps({"i": i}) + "\n" for i in range(20)), encoding="utf-8")
    tab = make_tab(path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"i": 20}) + "\n")
    qtbot.waitUntil(lambda: tab.info_bar("file_changed") is not None, timeout=TIMEOUT)
    bar = tab.info_bar("file_changed")
    assert bar.kind() == "info" and bar.message().startswith("Fayl o'sdi (+")
    # an edit in place
    data = path.read_bytes()
    path.write_bytes(data.replace(b'{"i": 3}', b'{"i": 7}'))
    os.utime(path, None)
    qtbot.waitUntil(lambda: tab.info_bar("file_changed") is not None
                    and tab.info_bar("file_changed").message() == "Fayl tashqaridan o'zgartirildi.", timeout=TIMEOUT)
    bar = tab.info_bar("file_changed")
    assert bar.kind() == "warning"
    with qtbot.waitSignal(tab.session.index_finished, timeout=TIMEOUT):
        click_bar_button(bar, "Qayta ochish")
    assert tab.info_bar("file_changed") is None
    assert tab.session.doc.record_count == 21


def test_file_changed_ignore(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(3)))
    tab.session.file_changed.emit("changed")
    bar = tab.info_bar("file_changed")
    click_bar_button(bar, "E'tiborsiz qoldirish")
    assert tab.info_bar("file_changed") is None
    assert tab.session.state == "ready"


def test_open_failure_bar_and_retry(qtbot, make_tab, tmp_path) -> None:
    path = tmp_path / "later.json"
    tab = make_tab(path)
    assert tab.session.state == "failed" and tab.failed
    bar = tab.info_bar("open_failed")
    assert bar is not None and bar.kind() == "error"
    assert "Fayl topilmadi" in bar.message()
    assert [b.text() for b in bar.buttons()] == ["Qayta urinish", "Logni ochish"]
    write_json(path, records(4))
    click_bar_button(bar, "Qayta urinish")
    wait_state(qtbot, tab.session, "ready")
    assert tab.info_bar("open_failed") is None
    assert tab.session.doc.record_count == 4


def test_wrong_record_path_offers_the_record_path_dialog(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(wrapped_file(tmp_path / "w.json", 5), record_path="nope")
    assert tab.session.state == "ready" and tab.session.doc.error.code == "target_not_found"
    bar = tab.info_bar("json_error")
    assert bar is not None and "topilmadi" in bar.message()
    with qtbot.waitSignal(tab.record_path_dialog_opened, timeout=TIMEOUT) as blocker:
        click_bar_button(bar, "O'zgartirish…")
    dialog = blocker.args[0]
    assert dialog.chosen() == ("nope", None)
    dialog.choose_row(0)                                      # automatic detection
    with qtbot.waitSignal(tab.session.index_finished, timeout=TIMEOUT):
        dialog.accept()
    assert tab.session.doc.info.record_path == ("data",) and tab.session.doc.error is None
    assert tab.info_bar("json_error") is None


def test_open_failure_with_forced_options_offers_automatic(qtbot, make_tab, tmp_path) -> None:
    path = tmp_path / "plain.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    tab = make_tab(path, format="keyed")
    assert tab.session.state == "failed"
    bar = tab.info_bar("open_failed")
    assert "Avtomatik aniqlash bilan ochish" in [b.text() for b in bar.buttons()]
    click_bar_button(bar, "Avtomatik aniqlash bilan ochish")
    wait_state(qtbot, tab.session, "ready")
    assert tab.session.doc.kind == "array" and tab.session.format is None


def test_cancelled_open_shows_a_retry_bar(qtbot, app, make_tab, tmp_path) -> None:
    path = tmp_path / "big.json"
    path.write_bytes(b"[" + b",".join(b'{"i":%d}' % i for i in range(300_000)) + b"]")
    tab = make_tab(path, wait=False)
    assert tab.busy and tab.title() == f"{BUSY_MARK} big.json"
    app.tasks.cancel_all(owner=tab.session)
    wait_state(qtbot, tab.session, "failed")
    bar = tab.info_bar("open_failed")
    assert bar.kind() == "warning" and "bekor qilindi" in bar.message()
    click_bar_button(bar, "Qayta urinish")
    wait_state(qtbot, tab.session, "ready")
    assert tab.title() == "big.json"
    assert tab.session.doc.record_count == 300_000


def test_title_and_tooltip(qtbot, make_tab, tmp_path) -> None:
    path = write_json(tmp_path / "a.json", records(3))
    tab = make_tab(path, start=False)
    assert tab.title().startswith(BUSY_MARK)                  # not opened yet
    titles = []
    tab.title_changed.connect(lambda: titles.append(tab.title()))
    tab.session.start()
    wait_ready(qtbot, tab.session)
    assert tab.title() == "a.json" and titles[-1] == "a.json"
    assert tab.tooltip() == os.path.normpath(str(path))


# ----------------------------------------------------------------------------------------
# shutdown
# ----------------------------------------------------------------------------------------
def test_shutdown_closes_everything(qtbot, app, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(10)))
    views = [tab.panes[0].current_view(), tab.panes[1].current_view(), tab.set_active_view_kind("raw")]
    tab.shutdown()
    assert tab.closed and tab.session.state == "closed"
    for view in views:
        assert ("shutdown",) in view.calls
    tab.session.file_changed.emit("changed")                 # nothing reacts any more
    tab._on_open_failed(RuntimeError("late"))
    assert tab.info_bar_keys() == []
    tab.shutdown()                                            # idempotent


def test_shutdown_while_indexing(qtbot, app, make_tab, tmp_path) -> None:
    path = tmp_path / "big.json"
    path.write_bytes(b"[" + b",".join(b'{"i":%d}' % i for i in range(200_000)) + b"]")
    tab = make_tab(path, wait=False)
    tab.shutdown()
    assert tab.session.state == "closed"
    assert app.tasks.wait_idle(30)
    assert tab.info_bar_keys() == [] and not app.errors


def test_viewpane_standalone(qtbot, app, tmp_path) -> None:
    session = DocSession(app, tmp_path / "x.json")
    pane = ViewPane(session, view_registry=stub_registry(), index=0)
    qtbot.addWidget(pane)
    assert pane.current_kind() is None and pane.current_view() is None
    seen = []
    pane.view_changed.connect(seen.append)
    view = pane.set_view("schema")
    assert seen == ["schema"] and pane.current_view() is view
    pane.set_view("schema")
    assert seen == ["schema"]                                 # unchanged kind: no signal
    pane.shutdown()
    assert ("shutdown",) in view.calls
    session.close()
