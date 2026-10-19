"""Editing in the application shell with the REAL edit controller (ARCHITECTURE §12.9): close
flows answered through ``editing.ask_save_handler`` (save / discard / cancel; the window
closing with two dirty tabs; headless keeps the layer and the next open offers it), the
recovery bar for a layer left by a killed subprocess (restore / discard / save as JSON
Patch), Save and Save As from the window (titles follow), Undo / Redo labels, the status
bar's "● N o'zgarish" and "tahrirlangan", the "view outdated -- Refresh" bar, an export of a
dirty document (the edited content, the dialog says so; node exports of composite nodes),
search results after edits, Ctrl+B bookmarks across a structural edit.

Views are stubs (the shell is under test); sessions, controllers, the edit layer and files
are real (``JSONRESHAPE_HOME`` = the test's temporary folder)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from guishell_helpers import TIMEOUT, make_app, records, stub_panels, stub_registry, wait_ready, write_json
from jsonreshape.gui import editing, widgets
from jsonreshape.gui import session as session_mod
from jsonreshape.gui.doctab import DIRTY_MARK, DocumentTab
from jsonreshape.gui.exportdialog import ExportDialog, ExportReportDialog, export_node
from jsonreshape.gui.mainwindow import MainWindow
from jsonreshape.gui.navigation import BookmarksDialog
from jsonreshape.gui.searchpanel import SearchPanel
from jsonreshape.gui.session import FilterSpec, NodeRef
from jsonreshape.i18n import t

pytestmark = pytest.mark.gui

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------------
@pytest.fixture
def app(qapp, tmp_path, lang_uz):
    ctx = make_app(tmp_path)
    ctx.tasks.set_poll_interval(10)
    ctx.statuses = []
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    yield ctx
    for w in list(widgets._kept_alive):
        w.close()
    ctx.tasks.cancel_all()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def window(qtbot, app):
    w = MainWindow(app, view_registry=stub_registry(), panel_factories=stub_panels())
    qtbot.addWidget(w)
    yield w
    if not w._closing:
        w.close_without_questions()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def answers(monkeypatch):
    """``answers[name] = choice`` answers the close question of the file ``name``
    (``answers["*"]``: any file); every question is recorded in ``answers.asked``."""

    class Answers(dict):
        asked: list[tuple[str, tuple]] = []

    got = Answers()
    got.asked = []

    def handler(parent, name, choices):
        got.asked.append((name, tuple(choices)))
        return got.get(name, got.get("*", "cancel"))

    monkeypatch.setattr(editing, "ask_save_handler", handler)
    return got


def open_editable(qtbot, window: MainWindow, path, options=None) -> DocumentTab:
    tab = window.open_path(path, options)
    wait_ready(qtbot, tab.session)
    assert tab.session.state == "ready", tab.session.open_error
    qtbot.waitUntil(lambda: tab.edit is not None and (tab.edit.available or bool(tab.edit.reason)),
                    timeout=TIMEOUT)
    return tab


def wait_available(qtbot, tab: DocumentTab) -> None:
    qtbot.waitUntil(lambda: tab.edit is not None and tab.edit.available and not tab.edit.busy, timeout=TIMEOUT)


def run(qtbot, fn, *args, **kw):
    """A controller command; waits for its ``on_done`` and returns what it got."""
    got: list = []
    ok = fn(*args, on_done=got.append, **kw)
    assert ok is not False, "the command was refused"
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    return got[0]


def people(path: Path, n: int = 30) -> Path:
    """``n`` records; record 4 holds the exact number token ``1.10`` (and ``-0.0``)."""
    write_json(path, [{"id": i, "name": f"n{i}", "city": "Toshkent" if i % 3 == 0 else "Xiva",
                       "num": "@@4@@" if i == 4 else i} for i in range(n)])
    path.write_text(path.read_text(encoding="utf-8").replace('"@@4@@"', "1.10, \"neg\": -0.0"), encoding="utf-8")
    return path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def edit_name(qtbot, tab: DocumentTab, record: int, text: str) -> None:
    wait_available(qtbot, tab)
    cs = run(qtbot, tab.edit.set_value, NodeRef(record, ("name",)), json.dumps(text, ensure_ascii=False))
    assert cs is not None
    qtbot.waitUntil(lambda: tab.dirty, timeout=TIMEOUT)


def kill_after_edits(path: Path, edits: list[tuple[int, str]], tmp_path: Path) -> None:
    """A subprocess edits ``path`` (``set_value`` of ``name`` per (record, text)) and is killed
    (``os._exit``) without closing anything: its layer stays on disk."""
    script = tmp_path / "killed_editor.py"
    script.write_text(textwrap.dedent(f"""
        import json, os, sys
        sys.path.insert(0, {str(ROOT)!r})
        from jsonreshape.reader import Document
        from jsonreshape.edits import EditSession, Ref
        doc = Document.open(sys.argv[1], use_cache=False)
        doc.build_index()
        es = EditSession.open(doc)
        for record, text in json.loads(sys.argv[2]):
            es.set_value(Ref(record, ("name",)), json.dumps(text, ensure_ascii=False))
        os._exit(1)
    """), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-I", str(script), str(path), json.dumps(edits)], capture_output=True,
                          text=True, timeout=120, env=dict(os.environ), stdin=subprocess.DEVNULL)
    assert proc.returncode == 1, proc.stderr


# ----------------------------------------------------------------------------------------
# close flows (ask_save_handler)
# ----------------------------------------------------------------------------------------
def test_close_tab_cancel_then_save(qtbot, app, window, answers, tmp_path) -> None:
    path = people(tmp_path / "a.json")
    original = path.read_bytes()
    tab = open_editable(qtbot, window, path)
    edit_name(qtbot, tab, 1, "Ўзбекистон O'zbek")
    assert window.tabs.tabText(0) == f"{DIRTY_MARK}a.json"
    answers["a.json"] = "cancel"
    assert window.close_tab() is False
    qtbot.waitUntil(lambda: bool(answers.asked), timeout=TIMEOUT)
    qtbot.wait(100)
    assert not tab.closed and tab.dirty and path.read_bytes() == original
    assert answers.asked[-1] == ("a.json", ("save", "discard", "cancel"))
    answers["a.json"] = "save"
    window.close_tab()
    qtbot.waitUntil(lambda: tab.closed, timeout=TIMEOUT)
    assert read_json(path)[1]["name"] == "Ўзбекистон O'zbek"
    assert Path(str(path) + ".bak").read_bytes() == original
    # nothing is pending afterwards
    again = open_editable(qtbot, window, path)
    wait_available(qtbot, again)
    assert again.info_bar("recovery") is None and not again.dirty
    assert app.errors == []


def test_close_tab_discard_deletes_the_layer(qtbot, app, window, answers, tmp_path) -> None:
    path = people(tmp_path / "a.json")
    original = path.read_bytes()
    tab = open_editable(qtbot, window, path)
    edit_name(qtbot, tab, 2, "x")
    answers["*"] = "discard"
    window.close_tab()
    qtbot.waitUntil(lambda: tab.closed, timeout=TIMEOUT)
    assert path.read_bytes() == original
    again = open_editable(qtbot, window, path)
    wait_available(qtbot, again)
    assert again.info_bar("recovery") is None and not again.dirty and again.edit.pending == []


def test_window_close_with_two_dirty_tabs(qtbot, app, window, answers, tmp_path) -> None:
    a = people(tmp_path / "a.json")
    b = people(tmp_path / "b.json", 10)
    b_original = b.read_bytes()
    tab_a = open_editable(qtbot, window, a)
    tab_b = open_editable(qtbot, window, b)
    edit_name(qtbot, tab_a, 0, "A")
    edit_name(qtbot, tab_b, 0, "B")
    answers["a.json"] = "save"
    answers["b.json"] = "cancel"
    window.close()
    qtbot.waitUntil(lambda: len(answers.asked) == 2, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not window._close_chain, timeout=TIMEOUT)
    qtbot.wait(100)
    assert not window._closing and not tab_b.closed and tab_b.dirty      # Cancel: the window stays
    assert read_json(a)[0]["name"] == "A"                                # the first answer was carried out
    # the saved tab shows its saved file again and can be edited
    qtbot.waitUntil(lambda: tab_a.session.state == "ready" and tab_a.edit is not None and tab_a.edit.available,
                    timeout=TIMEOUT)
    assert not tab_a.dirty and tab_a.session.doc.pinned().record(0)["name"] == "A"
    answers["b.json"] = "discard"
    window.close()
    qtbot.waitUntil(lambda: window._closing, timeout=TIMEOUT)
    assert b.read_bytes() == b_original
    assert [name for name, _ in answers.asked].count("b.json") == 2


def test_headless_close_keeps_the_layer_for_recovery(qtbot, app, window, tmp_path) -> None:
    assert editing.ask_save_handler is None and app.headless
    path = people(tmp_path / "a.json")
    tab = open_editable(qtbot, window, path)
    edit_name(qtbot, tab, 3, "kept")
    window.close()                               # headless, no handler: closes, the layer stays
    assert window._closing and tab.closed
    assert app.tasks.wait_idle(30)
    other = MainWindow(app, view_registry=stub_registry(), panel_factories=stub_panels())
    qtbot.addWidget(other)
    try:
        again = open_editable(qtbot, other, path)
        qtbot.waitUntil(lambda: again.info_bar("recovery") is not None, timeout=TIMEOUT)
        bar = again.info_bar("recovery")
        assert t("gui.doctab.recovery.restore") in [b.text() for b in bar.buttons()]
        assert again.edit.reason == "recovery_pending"
        again.restore_pending(again.pending()[0])
        qtbot.waitUntil(lambda: again.dirty and again.edit.available, timeout=TIMEOUT)
        assert again.info_bar("recovery") is None
        assert again.session.doc.pinned().record(3)["name"] == "kept"
        assert other.tabs.tabText(0) == f"{DIRTY_MARK}a.json"
    finally:
        other.close_without_questions()


# ----------------------------------------------------------------------------------------
# recovery bar: a layer left by a killed process
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("action", ["restore", "discard", "patch"])
def test_recovery_of_a_killed_process(qtbot, app, window, tmp_path, monkeypatch, action) -> None:
    path = people(tmp_path / "a.json")
    original = path.read_bytes()
    kill_after_edits(path, [(1, "birinchi"), (5, "beshinchi")], tmp_path)
    tab = open_editable(qtbot, window, path)
    qtbot.waitUntil(lambda: tab.info_bar("recovery") is not None, timeout=TIMEOUT)
    bar = tab.info_bar("recovery")
    assert "2" in bar.message() and bar.kind() == "warning"
    assert not tab.edit.available and tab.edit.reason == "recovery_pending"
    assert not window.action("undo").isEnabled() and not window.action("edit_edit_value").isEnabled()
    info = tab.pending()[0]
    if action == "restore":
        tab.restore_pending(info)
        qtbot.waitUntil(lambda: tab.dirty and tab.edit.available, timeout=TIMEOUT)
        doc = tab.session.doc.pinned()
        assert doc.record(1)["name"] == "birinchi" and doc.record(5)["name"] == "beshinchi"
        assert tab.edit.steps == 2 and window.action("undo").isEnabled()
        assert window.action("undo").text() == t("gui.main.act.undo_step", label=t("edits.step.set_value"))
    elif action == "discard":
        monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: True)
        tab.discard_pending(info)
        qtbot.waitUntil(lambda: tab.info_bar("recovery") is None and tab.edit.available, timeout=TIMEOUT)
        assert not tab.dirty and tab.session.doc.pinned().record(1)["name"] == "n1"
    else:
        out = tmp_path / "saved.patch.json"
        tab.ask_save_path = lambda title, default, filters: str(out)
        tab.export_pending(info)
        qtbot.waitUntil(lambda: out.exists() and out.stat().st_size > 0, timeout=TIMEOUT)
        assert app.tasks.wait_idle(30)
        ops = read_json(out)
        assert {op["op"] for op in ops} <= {"replace", "add", "remove", "test"}
        assert any(op.get("value") == "birinchi" for op in ops)
        assert tab.info_bar("recovery") is not None            # the layer stays until discarded
    assert path.read_bytes() == original
    assert app.errors == []


# ----------------------------------------------------------------------------------------
# save / save as / undo from the window; status bar
# ----------------------------------------------------------------------------------------
def test_save_in_place_and_save_as(qtbot, app, window, tmp_path) -> None:
    path = people(tmp_path / "a.json")
    tab = open_editable(qtbot, window, path)
    edit_name(qtbot, tab, 1, "saqlandi")
    assert window.action("save").isEnabled()
    window.action("save").trigger()
    qtbot.waitUntil(lambda: not tab.dirty and tab.session.state == "ready" and tab.edit is not None
                    and tab.edit.available, timeout=TIMEOUT)
    assert read_json(path)[1]["name"] == "saqlandi"
    assert window.tabs.tabText(0) == "a.json" and window.windowTitle() == "a.json — JSON Reshape"
    # Save As: the tab follows the new file
    edit_name(qtbot, tab, 2, "nusxa")
    target = tmp_path / "b.json"
    assert window.save_as_current(target) is True
    qtbot.waitUntil(lambda: tab.session.name == "b.json" and not tab.dirty and tab.session.state == "ready",
                    timeout=TIMEOUT)
    assert read_json(target)[2]["name"] == "nusxa" and read_json(path)[2]["name"] == "n2"
    qtbot.waitUntil(lambda: window.tabs.tabText(0) == "b.json", timeout=TIMEOUT)
    assert window.tabs.tabToolTip(0) == str(target) and window.windowTitle() == "b.json — JSON Reshape"
    assert app.errors == []


def test_undo_redo_and_status_bar(qtbot, app, window, tmp_path) -> None:
    path = people(tmp_path / "a.json")
    tab = open_editable(qtbot, window, path)
    bar = window.status
    edit_name(qtbot, tab, 1, "bir")
    edit_name(qtbot, tab, 2, "ikki")
    qtbot.waitUntil(lambda: bar.dirty_text() == "● 2 o'zgarish", timeout=TIMEOUT)
    assert not bar.dirty_label.isHidden()
    window.action("undo").trigger()
    qtbot.waitUntil(lambda: bar.dirty_text() == "● 1 o'zgarish", timeout=TIMEOUT)
    wait_available(qtbot, tab)
    assert window.action("redo").isEnabled()
    assert window.action("redo").text() == t("gui.main.act.redo_step", label=t("edits.step.set_value"))
    window.action("redo").trigger()
    qtbot.waitUntil(lambda: bar.dirty_text() == "● 2 o'zgarish", timeout=TIMEOUT)
    # "tahrirlangan" for a node of an edited record, a byte range for the others
    tab.session.select(NodeRef(1, ("name",)))
    qtbot.waitUntil(lambda: bar.offset_edited(), timeout=TIMEOUT)
    assert bar.offset_label.full_text() == t("gui.status.offset.edited") and bar.offset_span() is None
    tab.session.select(NodeRef(7, ("name",)))
    qtbot.waitUntil(lambda: bar.offset_span() is not None, timeout=TIMEOUT)
    assert not bar.offset_edited()
    # the root of an edited array is built from edited records: no byte range either
    tab.session.select(NodeRef(None, ()))
    qtbot.waitUntil(lambda: bar.offset_edited(), timeout=TIMEOUT)
    # counts are the effective ones
    wait_available(qtbot, tab)
    run(qtbot, tab.edit.delete, [NodeRef(0, ())])
    qtbot.waitUntil(lambda: bar.count_label.full_text() == "29 yozuv", timeout=TIMEOUT)


def test_view_outdated_bar_after_a_big_structural_edit(qtbot, app, window, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(session_mod, "REMAP_INLINE_MAX", 0)
    monkeypatch.setattr(session_mod, "REMAP_GUI_MAX", 0)
    path = people(tmp_path / "a.json", 40)
    tab = open_editable(qtbot, window, path)
    s = tab.session
    s.set_filter(FilterSpec("city == 'Toshkent'"))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 14, timeout=TIMEOUT)
    wait_available(qtbot, tab)
    run(qtbot, tab.edit.delete, [NodeRef(0, ())])
    qtbot.waitUntil(lambda: tab.info_bar("view_outdated") is not None, timeout=TIMEOUT)
    assert s.view_outdated and s.view_len() == 39
    bar = tab.info_bar("view_outdated")
    for button in bar.buttons():
        if button.text() == t("gui.doctab.view_outdated.refresh"):
            button.click()
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 13, timeout=TIMEOUT)
    assert tab.info_bar("view_outdated") is None


# ----------------------------------------------------------------------------------------
# export / search / bookmarks of an edited document
# ----------------------------------------------------------------------------------------
def test_export_of_a_dirty_document(qtbot, app, window, tmp_path) -> None:
    path = people(tmp_path / "a.json", 12)
    tab = open_editable(qtbot, window, path)
    edit_name(qtbot, tab, 3, "Ўзгарди")
    wait_available(qtbot, tab)
    run(qtbot, tab.edit.delete, [NodeRef(0, ())])
    dialog = ExportDialog(tab.session, window, scope="all")
    qtbot.addWidget(dialog)
    assert dialog.edited() and not dialog.edited_label.isHidden()
    out = tmp_path / "all.jsonl"
    dialog.set_output_path(out)
    with qtbot.waitSignal(dialog.export_finished, timeout=TIMEOUT) as blocker:
        assert dialog.start_export() is not None
    assert blocker.args[0].edited
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 11 and lines[0]["id"] == 1 and lines[2]["name"] == "Ўзгарди"
    assert t("gui.export.report.edited") in ExportReportDialog(blocker.args[0]).summary_text()
    # node exports of composite nodes read the effective records
    root = tmp_path / "root.json"
    export_node(tab.session, NodeRef(None, ()), root, "json", indent=None)
    data = read_json(root)
    assert len(data) == 11 and data[2]["name"] == "Ўзгарди" and data[0]["id"] == 1
    lines_out = tmp_path / "root.jsonl"
    report = export_node(tab.session, NodeRef(None, ()), lines_out, "jsonl")
    assert report.records == 11
    assert [json.loads(x)["id"] for x in lines_out.read_text(encoding="utf-8").splitlines()] == list(range(1, 12))
    # exact numbers survive
    text = root.read_text(encoding="utf-8")
    assert '"num":1.10' in text and '"neg":-0.0' in text
    assert app.errors == []


def test_export_pins_the_state_it_started_with(qtbot, app, window, tmp_path, monkeypatch) -> None:
    import threading

    from jsonreshape import writer

    gate = threading.Event()
    real = writer.export

    def slow(doc, out, opts, source=None, *, progress=None, cancel=None):
        assert gate.wait(30)
        return real(doc, out, opts, source, progress=progress, cancel=cancel)

    monkeypatch.setattr(writer, "export", slow)
    path = people(tmp_path / "a.json", 8)
    tab = open_editable(qtbot, window, path)
    edit_name(qtbot, tab, 1, "oldin")
    dialog = ExportDialog(tab.session, window, scope="all")
    qtbot.addWidget(dialog)
    out = tmp_path / "pinned.json"
    dialog.set_output_path(out)
    dialog.start_export()
    edit_name(qtbot, tab, 1, "keyin")                       # edited while the export waits
    with qtbot.waitSignal(dialog.export_finished, timeout=TIMEOUT):
        gate.set()
    assert read_json(out)[1]["name"] == "oldin"


def test_search_results_follow_edits(qtbot, app, window, tmp_path, monkeypatch) -> None:
    path = people(tmp_path / "a.json", 30)
    tab = open_editable(qtbot, window, path)
    panel = SearchPanel(app)
    qtbot.addWidget(panel)
    panel.set_session(tab.session)
    panel.set_query_text("Toshkent")
    with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT):
        panel.run_search()
    st = panel.state()
    assert [h.record for h in st.hits] == list(range(0, 30, 3))
    # a structural edit before the hits: they move with their records
    wait_available(qtbot, tab)
    run(qtbot, tab.edit.delete, [NodeRef(1, ())])
    st = panel.state()
    assert st.outdated and [h.record for h in st.hits] == [0] + [r - 1 for r in range(3, 30, 3)]
    assert t("gui.search.outdated") in panel.summary_text()
    assert panel.activate_hit(1) == NodeRef(2, ("city",))
    assert tab.session.current == NodeRef(2, ("city",))
    # deleting a record with hits drops them
    run(qtbot, tab.edit.delete, [NodeRef(0, ())])
    assert [h.record for h in panel.state().hits] == [r - 2 for r in range(3, 30, 3)]
    # the records with hits are built from the moved hits; the search of all records exports again
    assert panel.open_button.isEnabled() and panel.export_button.isEnabled()
    with qtbot.waitSignal(panel.records_opened, timeout=TIMEOUT):
        panel.open_as_records()
    qtbot.waitUntil(lambda: not tab.session.view_busy, timeout=TIMEOUT)
    assert [tab.session.record_at(p) for p in range(tab.session.view_len())] == [r - 2 for r in range(3, 30, 3)]
    # a reset (discard all changes) makes the results stale
    tab.session.reset_view()
    wait_available(qtbot, tab)
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: True)
    tab.edit.discard(window)
    qtbot.waitUntil(lambda: not tab.dirty, timeout=TIMEOUT)
    assert panel.state().status == "stale" and panel.state().hits == []
    panel.shutdown()
    assert app.errors == []


def test_bookmarks_follow_structural_edits(qtbot, app, window, tmp_path) -> None:
    path = people(tmp_path / "a.json", 20)
    tab = open_editable(qtbot, window, path)
    window.show()
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window, timeout=TIMEOUT)
    tab.session.select(NodeRef(5, ("name",)))
    window._ask_text = lambda title, label, default="": "beshinchi"
    window.tabs.tabBar().setFocus()
    qtbot.waitUntil(lambda: window.tabs.tabBar().hasFocus(), timeout=TIMEOUT)
    qtbot.keyClick(window.tabs.tabBar(), Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    assert [b.name for b in tab.session.bookmarks] == ["beshinchi"]
    dialog = BookmarksDialog(tab.session, window)
    qtbot.addWidget(dialog)
    assert dialog.tree.topLevelItem(0).text(1) == "#5.name"
    wait_available(qtbot, tab)
    run(qtbot, tab.edit.delete, [NodeRef(2, ())])
    qtbot.waitUntil(lambda: dialog.tree.topLevelItem(0).text(1) == "#4.name", timeout=TIMEOUT)
    run(qtbot, tab.edit.delete, [NodeRef(4, ())])
    qtbot.waitUntil(lambda: dialog.tree.topLevelItem(0).text(1) == t("gui.nav.bookmarks.deleted"), timeout=TIMEOUT)
    window.action("undo").trigger()
    qtbot.waitUntil(lambda: dialog.tree.topLevelItem(0).text(1) == "#4.name", timeout=TIMEOUT)
    with qtbot.waitSignal(dialog.resolved, timeout=TIMEOUT):
        dialog.go(0)
    assert tab.session.current.record == 4


def test_bulk_dialog_from_the_window(qtbot, app, tmp_path) -> None:
    from jsonreshape.gui.bulkdialog import BulkDialog

    panels = stub_panels()
    panels.pop("bulk")                            # the real dialog (lazy default factory)
    w = MainWindow(app, view_registry=stub_registry(), panel_factories=panels)
    qtbot.addWidget(w)
    try:
        tab = open_editable(qtbot, w, people(tmp_path / "a.json", 12))
        wait_available(qtbot, tab)
        w._update_actions()
        assert w.action("bulk").isEnabled()
        w.action("bulk").trigger()                # Ctrl+H
        dialog = w.dialogs["bulk"]
        assert isinstance(dialog, BulkDialog) and dialog.session is tab.session
        assert dialog.scope_choice() == "all"
        assert w.show_bulk() is dialog            # one per document
        w.close_tab()
        qtbot.waitUntil(lambda: not _alive(dialog) or not dialog.isVisible(), timeout=TIMEOUT)
    finally:
        w.close_without_questions()


def _alive(obj) -> bool:
    from shiboken6 import isValid

    return isValid(obj)


def test_node_export_of_an_edited_wrapped_document(qtbot, app, window, tmp_path) -> None:
    path = tmp_path / "w.json"
    path.write_text('{"meta": {"v": 1, "who": "Ali"},\n "data": [{"id": 0, "x": 1.10}, {"id": 1, "x": -0.0},'
                    ' {"id": 2, "x": 12345678901234567890}],\n "tail": [1, 2]}\n', encoding="utf-8")
    tab = open_editable(qtbot, window, path, {"format": "wrapped", "record_path": "data"})
    wait_available(qtbot, tab)
    run(qtbot, tab.edit.set_value, NodeRef(1, ("x",)), '"Oʻzbek"')
    wait_available(qtbot, tab)
    run(qtbot, tab.edit.set_value, NodeRef(None, ("meta", "who")), '"Vali"')
    # the wrapper (an ancestor of the record container): effective text
    out = tmp_path / "root.json"
    export_node(tab.session, NodeRef(None, ()), out, "json", indent=None)
    data = read_json(out)
    assert data["meta"]["who"] == "Vali" and data["data"][1]["x"] == "Oʻzbek" and data["tail"] == [1, 2]
    text = out.read_text(encoding="utf-8")
    assert "1.10" in text and "12345678901234567890" in text
    # the record container as JSON Lines: one effective record per line
    lines = tmp_path / "data.jsonl"
    report = export_node(tab.session, NodeRef(None, ("data",)), lines, "jsonl")
    got = [json.loads(x) for x in lines.read_text(encoding="utf-8").splitlines()]
    assert report.records == 3 and [g["id"] for g in got] == [0, 1, 2] and got[1]["x"] == "Oʻzbek"
    # the wrapper as one JSON line
    one = tmp_path / "root.jsonl"
    export_node(tab.session, NodeRef(None, ()), one, "jsonl")
    assert len(one.read_text(encoding="utf-8").splitlines()) == 1
    assert read_json(one)["meta"]["who"] == "Vali"
    assert app.errors == []


def test_real_views_register_no_view_keys_as_window_shortcuts(qtbot, app, tmp_path) -> None:
    """With every real view created in both panes: Enter, Shift+Enter, F2, Ins, Del, Ctrl+D,
    Ctrl+V and Alt+Up/Down are never shortcuts of the window or of the document area (they
    belong to the grids' key handlers; a shortcut local to one widget is allowed)."""
    from PySide6.QtGui import QAction, QKeySequence, QShortcut

    from jsonreshape.gui.doctab import VIEW_KINDS

    keys = ("Return", "Enter", "Shift+Return", "Shift+Enter", "F2", "Ins", "Del", "Ctrl+D", "Ctrl+V", "Alt+Up",
            "Alt+Down")
    forbidden = {QKeySequence(k).toString() for k in keys}
    wide = (Qt.ShortcutContext.WindowShortcut, Qt.ShortcutContext.ApplicationShortcut,
            Qt.ShortcutContext.WidgetWithChildrenShortcut)
    w = MainWindow(app, panel_factories=stub_panels())          # the default (real) views
    qtbot.addWidget(w)
    try:
        w.show()
        tab = open_editable(qtbot, w, people(tmp_path / "a.json", 20))
        for pane in tab.panes:
            for kind in VIEW_KINDS:
                pane.set_view(kind)
        qtbot.wait(200)
        bad = []
        for action in w.findChildren(QAction):
            if action.shortcutContext() in wide:
                bad += [(action.objectName(), s.toString()) for s in action.shortcuts() if s.toString() in forbidden]
        for shortcut in w.findChildren(QShortcut):
            if shortcut.context() in wide and shortcut.key().toString() in forbidden:
                bad.append((shortcut.objectName(), shortcut.key().toString()))
        assert bad == []
    finally:
        w.close_without_questions()
