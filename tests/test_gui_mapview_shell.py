"""The structure builder in the application shell (docs/ARCHITECTURE.md §13.5).

* the export dialog's mapping mode (gui/exportdialog.py): the file written is exactly what ``engine.apply`` writes
  for the same tree and options (array / JSON Lines / wrapped / keyed / single, indent, JSON Lines output, the
  filtered view as the scope, a tree copied when the export starts), the widgets of the mode (formats, scope,
  options, default name ``<name>_yangi.json``, "Ixcham"), switching the mapping off (the ordinary export), very large
  records (stop / skip), the report;
* the main window: Ko'rinish ▸ Tuzilma quruvchi (Ctrl+6); Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z and the Edit menu's undo /
  redo act on the designer while the mapping view is active (the document's edit undo is not touched) and on the
  document again in another view; Ctrl+E opens the export in mapping mode;
* the document tab: the schema view's "Tuzilma quruvchi…" opens the builder; the view's history changes reach the
  window (``view_state_changed``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from guishell_helpers import (  # noqa: E402
    MappingStub,
    SchemaStub,
    TreeStub,
    install_stub_edit,
    make_app,
    mapping_registry,
    stub_panels,
    wait_ready,
    write_json,
)
from jsonreshape import engine, writer  # noqa: E402
from jsonreshape import mapping as mp  # noqa: E402
from jsonreshape.gui import widgets  # noqa: E402
from jsonreshape.gui.doctab import DocumentTab  # noqa: E402
from jsonreshape.gui.exportdialog import ExportDialog, ExportOutcome, MappingSource  # noqa: E402
from jsonreshape.gui.mainwindow import MainWindow  # noqa: E402
from jsonreshape.gui.mapview import MapView  # noqa: E402
from jsonreshape.gui.session import DocSession, FilterSpec  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000
RECS = [{"id": i, "name": f"Ism {i}", "city": ["Toshkent", "Oʻzbekiston", "Самарқанд"][i % 3],
         "orders": [{"oid": i * 10 + k, "sum": 1.10} for k in range(i % 3)], "contact": {"phone": f"+998 {i}"}}
        for i in range(12)]


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = make_app(tmp_path)
    ctx.statuses = []
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    yield ctx
    for w in list(widgets._kept_alive):
        w.close()
    ctx.tasks.cancel_all()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def opened(qtbot, app):
    made: list[DocSession] = []

    def factory(path: Path, **kw: Any) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
            s.start()
        assert s.state == "ready", s.open_error
        return s

    yield factory
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def builder(qtbot):
    made: list[MapView] = []

    def make(session: DocSession) -> MapView:
        v = MapView(session)
        qtbot.addWidget(v)
        v.resize(1200, 700)
        v.show()
        qtbot.waitExposed(v)
        v.activate()
        qtbot.waitUntil(lambda: v.state.designer is not None, timeout=TIMEOUT)
        made.append(v)
        return v

    yield make
    for v in made:
        v.shutdown()


def node(view: MapView, ref: str) -> Any:
    n = view.state.tree.find(ref)
    assert n is not None, ref
    return n


def run_export(qtbot: Any, dlg: ExportDialog) -> ExportOutcome:
    with qtbot.waitSignal(dlg.export_finished, timeout=TIMEOUT) as blocker:
        assert dlg.start_export() is not None, dlg.error_text()
    return blocker.args[0]


def reference_output(session: DocSession, tree: Any, path: Path, **opts: Any) -> bytes:
    """What ``engine.apply`` writes for ``tree`` on the session's (pinned) document."""
    report = engine.apply(session.doc.pinned(), tree, path, engine.ApplyOptions(**opts))
    assert report.files == [path]
    return path.read_bytes()


# ----------------------------------------------------------------------------------------
# export dialog: mapping mode
# ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["array", "jsonl", "wrapped", "keyed", "single"])
def test_export_with_mapping_writes_what_engine_apply_writes(qtbot, opened, builder, tmp_path, kind) -> None:
    if kind == "array":
        path = write_json(tmp_path / "a.json", RECS)
    elif kind == "jsonl":
        path = tmp_path / "a.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in RECS), encoding="utf-8")
    elif kind == "wrapped":
        path = write_json(tmp_path / "w.json", {"meta": {"v": 1}, "data": RECS, "tail": "end"})
    elif kind == "keyed":
        path = write_json(tmp_path / "k.json", {f"id{r['id']}": r for r in RECS})
    else:
        path = write_json(tmp_path / "s.json", {"title": "bitta", "items": [1, 2], "inner": {"a": 1.10}})
    s = opened(path)
    assert s.doc.kind == kind
    v = builder(s)
    st = v.state
    # a few designer operations (a move, a rename, a gather where the document has orders)
    if kind == "single":
        st.run("rename", node(v, "title").id, "sarlavha")
        st.run("move", node(v, "inner.a").id, node(v, "items").id, "before")
    else:
        base = {"array": "[]", "jsonl": "[]", "wrapped": "data[]", "keyed": ""}[kind]
        prefix = base + "." if base else ""
        st.run("move", node(v, prefix + "contact.phone").id, node(v, prefix + "name").id, "before")
        st.run("rename", node(v, prefix + "city").id, "shahar")
        st.run("move", node(v, prefix + "orders[].oid").id, node(v, prefix + "id").id, "after")
    dlg = ExportDialog(s, v, mapping=MappingSource(tree=st.tree_copy, summary=st.stats_text()))
    qtbot.addWidget(dlg)
    assert dlg.mapping_active() and dlg.windowTitle().startswith("Tuzilma bo'yicha eksport")
    stem = path.stem
    assert dlg.output_path() == os.path.join(str(tmp_path), f"{stem}_yangi.json")
    out = tmp_path / "out" / "result.json"
    out.parent.mkdir()
    dlg.set_output_path(str(out))
    outcome = run_export(qtbot, dlg)
    want = reference_output(s, st.tree_copy(), tmp_path / "ref.json", indent=2)
    assert out.read_bytes() == want
    assert outcome.apply_report is not None and outcome.report.files == [out]
    assert outcome.report.records == outcome.apply_report.records_in
    # JSON Lines with the same mapping
    dlg.set_format("jsonl")
    assert dlg.output_path().endswith("result.jsonl")
    outcome = run_export(qtbot, dlg)
    want = reference_output(s, st.tree_copy(), tmp_path / "ref.jsonl", jsonl=True)
    assert Path(dlg.output_path()).read_bytes() == want
    assert not s.app.errors


def test_mapping_mode_widgets_and_switching_it_off(qtbot, opened, builder, tmp_path, monkeypatch) -> None:
    s = opened(write_json(tmp_path / "data.json", RECS))
    v = builder(s)
    v.compact_check.setChecked(True)
    dlg = v.export_with_mapping()
    assert isinstance(dlg, ExportDialog)
    qtbot.addWidget(dlg)
    assert dlg.mapping_check.isVisible() and dlg.mapping_check.isChecked()
    assert dlg.mapping_check.text() == "Tuzilma bo'yicha qayta qurish (8 maydon)"
    assert dlg.indent_combo.currentData() is None                     # "Ixcham" preselects the compact indent
    model = dlg.format_combo.model()
    enabled = {dlg.format_combo.itemData(i): model.item(i).isEnabled() for i in range(dlg.format_combo.count())}
    assert enabled == {"json": True, "jsonl": True, "csv": False, "xlsx": False, "sqlite": False}
    assert model.item(2).toolTip() == "Tuzilma bo'yicha eksport faqat JSON yoki JSON Lines bo'ladi."
    assert not dlg.scope_buttons["node"].isEnabled()
    assert not dlg.json_sort_combo.isEnabled() and not dlg.as_keyed_check.isEnabled()
    assert dlg.skip_huge_check.isVisible()
    assert dlg.output_path().endswith("data_yangi.json")
    # a typed .csv name does not switch the format in mapping mode (the extension warning says so)
    dlg.set_output_path(str(tmp_path / "x.csv"))
    assert dlg.format() == "json" and ".csv" in dlg.warning_label.text()
    dlg.set_output_path(str(tmp_path / "x.json"))
    assert dlg.export_button.isEnabled()
    # compact output = engine.apply with indent None
    out = run_export(qtbot, dlg)
    assert Path(out.path).read_bytes() == reference_output(s, v.state.tree_copy(), tmp_path / "r.json", indent=None)
    # the report window says it was restructured
    report = out.report
    assert report.rows == len(RECS) and report.records == len(RECS)
    assert dlg.report_dialog is not None
    assert dlg.report_dialog.summary_text().startswith("Tuzilma bo'yicha: 12 ta yozuv → 12 ta element (1 o'tish)")
    # off: the ordinary export (every format, the node scope, writer.export's output)
    dlg.mapping_check.setChecked(False)
    assert not dlg.mapping_active() and dlg.windowTitle().startswith("Eksport")
    assert all(model.item(i).isEnabled() for i in range(dlg.format_combo.count()))
    assert not dlg.skip_huge_check.isVisible() and dlg.json_sort_combo.isEnabled()
    target = tmp_path / "plain.json"
    dlg.set_output_path(str(target))
    run_export(qtbot, dlg)
    writer.export(s.doc.pinned(), tmp_path / "plain_ref.json", dlg.export_options())
    assert target.read_bytes() == (tmp_path / "plain_ref.json").read_bytes()
    # the remembered options of the ordinary export are not changed by a mapping export
    assert s.app.state.get("last_export_dir") == str(tmp_path)
    assert not s.app.errors


def test_mapping_export_of_a_filtered_view_and_a_tree_taken_at_start(qtbot, opened, builder, tmp_path) -> None:
    s = opened(write_json(tmp_path / "data.json", RECS))
    v = builder(s)
    s.set_filter(FilterSpec("id >= 6"))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 6, timeout=TIMEOUT)
    v.state.run("rename", node(v, "[].name").id, "ism")
    dlg = ExportDialog(s, v, mapping=MappingSource(tree=v.state.tree_copy))
    qtbot.addWidget(dlg)
    assert dlg.scope() == "view"
    out = tmp_path / "view.json"
    dlg.set_output_path(str(out))
    with qtbot.waitSignal(dlg.export_finished, timeout=TIMEOUT):
        assert dlg.start_export() is not None
        v.state.run("rename", node(v, "[].ism").id, "boshqa")          # after the start: not in this export
    got = json.loads(out.read_text(encoding="utf-8"))
    assert [r["id"] for r in got] == list(range(6, 12))
    assert all("ism" in r and "boshqa" not in r for r in got)


def test_mapping_export_of_very_large_records(qtbot, opened, builder, tmp_path, app) -> None:
    app.settings.large_node_bytes = 4096
    recs = [{"id": 1, "s": "a"}, {"id": 2, "s": "b" * 9000}, {"id": 3, "s": "c"}]
    s = opened(write_json(tmp_path / "h.json", recs))
    v = builder(s)
    dlg = ExportDialog(s, v, mapping=MappingSource(tree=v.state.tree_copy))
    qtbot.addWidget(dlg)
    out = tmp_path / "h_out.json"
    dlg.set_output_path(str(out))
    with qtbot.waitSignal(dlg.export_failed, timeout=TIMEOUT) as blocker:
        dlg.start_export()
    assert getattr(blocker.args[0], "key", "") == "engine.error.huge_record"
    assert not out.exists()
    app.errors.clear()
    dlg.skip_huge_check.setChecked(True)
    outcome = run_export(qtbot, dlg)
    assert [r["id"] for r in json.loads(out.read_text(encoding="utf-8"))] == [1, 3]
    assert outcome.report.skipped == 1 and any("o'tkazib yuborildi" in w for w in outcome.report.warnings)


def test_mapping_export_of_scalar_records(qtbot, opened, builder, tmp_path, app) -> None:
    s = opened(write_json(tmp_path / "scalars.json", [1, None, "Oʻzbekiston", 1.10, [2]]))
    v = builder(s)
    assert v.state.tree.find("[]").kind == "array"                     # [2]: the element node is an array
    s2 = opened(write_json(tmp_path / "scalars2.json", [1, None, "Oʻzbekiston"]))
    v2 = builder(s2)
    assert v2.state.tree.find("[]").kind == "value"
    dlg = ExportDialog(s2, v2, mapping=MappingSource(tree=v2.state.tree_copy))
    qtbot.addWidget(dlg)
    out = tmp_path / "scalars_out.json"
    dlg.set_output_path(str(out))
    finished: list = []
    failed: list = []
    dlg.export_finished.connect(finished.append)
    dlg.export_failed.connect(failed.append)
    assert dlg.start_export() is not None
    qtbot.waitUntil(lambda: bool(finished or failed), timeout=TIMEOUT)
    app.errors.clear()
    if failed:
        raise failed[0]
    assert json.loads(out.read_text(encoding="utf-8")) == [1, None, "Oʻzbekiston"]


# ----------------------------------------------------------------------------------------
# main window: view menu, undo / redo routing, Ctrl+E
# ----------------------------------------------------------------------------------------
@pytest.fixture
def window(qtbot, app):
    w = MainWindow(app, view_registry={**mapping_registry(), "mapping": MapView}, panel_factories=stub_panels())
    qtbot.addWidget(w)
    yield w
    w.close_without_questions()


def open_tab(qtbot: Any, window: MainWindow, path: Path) -> DocumentTab:
    tab = window.open_path(path)
    wait_ready(qtbot, tab.session)
    assert tab.session.state == "ready", tab.session.open_error
    return tab


def test_view_menu_entry_and_ctrl6(qtbot, window, tmp_path) -> None:
    tab = open_tab(qtbot, window, write_json(tmp_path / "a.json", RECS))
    act = window.action("view_mapping")
    assert act.shortcut() == QKeySequence("Ctrl+6") and act.text() == "Tuzilma quruvchi"
    assert act in window.menus["view"].actions()
    assert tab.panes[0].selector_kinds()[-1] == "mapping"
    act.trigger()
    assert tab.active_kind() == "mapping" and isinstance(tab.active_view(), MapView)
    assert act.isChecked()
    v = tab.active_view()
    qtbot.waitUntil(lambda: v.state.designer is not None, timeout=TIMEOUT)
    # the window's expansion commands act on the builder
    for key in ("expand_all", "collapse_all", "expand_level_1"):
        assert window.action(key).isEnabled(), key
    window.action("expand_level_1").trigger()
    assert [r.node.is_root for r in v.model.rows()] == [True, False] and not node(v, "[]").open
    window.action("expand_all").trigger()
    assert node(v, "[].orders").open


def test_undo_redo_go_to_the_designer_while_the_mapping_view_is_active(qtbot, app, tmp_path, monkeypatch) -> None:
    stubs = install_stub_edit(monkeypatch, available=True)
    w = MainWindow(app, view_registry={**mapping_registry(), "mapping": MapView}, panel_factories=stub_panels())
    qtbot.addWidget(w)
    try:
        w.show()
        tab = open_tab(qtbot, w, write_json(tmp_path / "a.json", RECS))
        qtbot.waitUntil(lambda: bool(stubs) and tab.edit is stubs[-1], timeout=TIMEOUT)
        edit = stubs[-1]
        edit.set(can_undo=True, can_redo=True, undo_text="Qiymat", redo_text="Qiymat", dirty=True)
        assert w.action("undo").isEnabled() and w.action("undo").text() == "&Ortga qaytarish: Qiymat"
        w.action("view_mapping").trigger()
        v = tab.active_view()
        qtbot.waitUntil(lambda: v.state.designer is not None, timeout=TIMEOUT)
        # the designer has no history yet: undo / redo disabled (with the reason), not the document's
        assert not w.action("undo").isEnabled() and not w.action("redo").isEnabled()
        assert "Tuzilma quruvchida" in w.action("undo").toolTip()
        v.state.run("rename", node(v, "[].name").id, "ism")
        assert w.action("undo").isEnabled() and w.action("undo").text() == "&Ortga qaytarish: tuzilma"
        w.action("undo").trigger()
        assert v.state.tree.find("[].name") is not None and "undo" not in edit.names()
        assert w.action("redo").isEnabled()
        w.action("redo").trigger()
        assert v.state.tree.find("[].ism") is not None and "redo" not in edit.names()
        # the window shortcut itself (Ctrl+Z) with the focus in the structure tree
        w.activateWindow()
        qtbot.waitUntil(lambda: QApplication.activeWindow() is w, timeout=TIMEOUT)
        v.tree.setFocus()
        qtbot.waitUntil(lambda: v.tree.hasFocus(), timeout=TIMEOUT)
        qtbot.keyClick(v.tree, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert v.state.tree.find("[].name") is not None
        qtbot.keyClick(v.tree, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        assert v.state.tree.find("[].ism") is not None
        qtbot.keyClick(v.tree, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        qtbot.keyClick(v.tree, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert v.state.tree.find("[].ism") is not None
        assert "undo" not in edit.names() and "redo" not in edit.names()
        # another view: the document's edits again
        w.action("view_tree").trigger()
        assert isinstance(tab.active_view(), TreeStub)
        assert w.action("undo").isEnabled() and w.action("undo").text() == "&Ortga qaytarish: Qiymat"
        w.action("undo").trigger()
        assert edit.names().count("undo") == 1
        assert v.state.tree.find("[].ism") is not None
    finally:
        w.close_without_questions()


def test_history_changes_of_a_view_reach_the_window(qtbot, app, tmp_path) -> None:
    w = MainWindow(app, view_registry=mapping_registry(), panel_factories=stub_panels())
    qtbot.addWidget(w)
    try:
        tab = open_tab(qtbot, w, write_json(tmp_path / "a.json", RECS))
        stub = tab.set_active_view_kind("mapping")
        assert isinstance(stub, MappingStub)
        assert not w.action("undo").isEnabled()
        seen = []
        tab.view_state_changed.connect(lambda: seen.append(1))
        stub.set_history(can_undo=True, text="tuzilma")
        assert seen and w.action("undo").isEnabled() and w.action("undo").text() == "&Ortga qaytarish: tuzilma"
        w.undo()
        assert stub.calls[-1] == ("undo",)
        stub.set_history(can_redo=True, text="tuzilma")
        assert w.action("redo").isEnabled() and not w.action("undo").isEnabled()
        w.redo()
        assert stub.calls[-1] == ("redo",)
        # Ctrl+E asks the active view's export with the mapping first
        assert w.show_export() == "mapping-export"
        tab.set_active_view_kind("tree")
        dialog = w.show_export()
        assert dialog is not None and dialog.args[0] is tab.session      # the ordinary (stub) export dialog
        dialog.close()
    finally:
        w.close_without_questions()


def test_the_builder_keys_reach_the_tree_inside_the_main_window(qtbot, window, tmp_path) -> None:
    """No window shortcut takes the structure tree's keys (F2, Del, Insert, Ctrl+X / Ctrl+V, Alt+↑,
    Ctrl+Shift+U, ←, Enter); Ctrl+Z / Ctrl+Y, ``*`` and Ctrl+C go through the window's actions to the view."""
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLineEdit

    K, M = Qt.Key, Qt.KeyboardModifier
    recs = [{"id": i, "name": f"n{i}", "statusComment": None, "contact": {"phone": "1", "city": "T"}} for i in range(5)]
    window.show()
    tab = open_tab(qtbot, window, write_json(tmp_path / "a.json", recs))
    window.action("view_mapping").trigger()
    v = tab.active_view()
    qtbot.waitUntil(lambda: v.state.designer is not None, timeout=TIMEOUT)
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window, timeout=TIMEOUT)
    st = v.state

    def focus(ref: str) -> None:
        assert v.select_node(node(v, ref).id)
        v.tree.setFocus()
        qtbot.waitUntil(v.tree.hasFocus, timeout=TIMEOUT)

    focus("[].name")
    QTest.keyClick(v.tree, K.Key_F2)
    qtbot.waitUntil(lambda: isinstance(QApplication.focusWidget(), QLineEdit), timeout=TIMEOUT)
    QTest.keyClick(QApplication.focusWidget(), K.Key_Escape)
    focus("[].statusComment")
    QTest.keyClick(v.tree, K.Key_Delete)
    assert st.tree.find("[].statusComment") is None
    focus("[].contact")
    QTest.keyClick(v.tree, K.Key_Insert)
    qtbot.waitUntil(lambda: isinstance(QApplication.focusWidget(), QLineEdit), timeout=TIMEOUT)
    QTest.keyClick(QApplication.focusWidget(), K.Key_Escape)
    assert st.tree.find("[].contact.yangi_guruh") is not None
    focus("[].id")
    QTest.keyClick(v.tree, K.Key_X, M.ControlModifier)
    focus("[].contact")
    QTest.keyClick(v.tree, K.Key_V, M.ControlModifier)
    assert [c.key for c in node(v, "[].contact").children] == ["phone", "city", "yangi_guruh", "id"]
    focus("[].contact.city")
    QTest.keyClick(v.tree, K.Key_Up, M.AltModifier)
    assert [c.key for c in node(v, "[].contact").children][:2] == ["city", "phone"]
    focus("[].contact")
    QTest.keyClick(v.tree, K.Key_U, M.ControlModifier | M.ShiftModifier)
    assert st.tree.find("[].contact") is None and st.tree.find("[].city") is not None
    steps = st.designer.changes
    QTest.keyClick(v.tree, K.Key_Z, M.ControlModifier)
    assert st.designer.changes == steps - 1 and st.tree.find("[].contact") is not None
    QTest.keyClick(v.tree, K.Key_Y, M.ControlModifier)
    assert st.designer.changes == steps and st.tree.find("[].contact") is None
    st.set_open(node(v, "").id, False)
    assert len(v.model.rows()) == 1
    QTest.keyClick(v.tree, K.Key_Asterisk)
    assert len(v.model.rows()) > 1 and node(v, "").open
    focus("[].name")
    QTest.keyClick(v.tree, K.Key_C, M.ControlModifier)
    qtbot.waitUntil(lambda: QApplication.clipboard().text() == "[].name", timeout=TIMEOUT)
    focus("[]")
    QTest.keyClick(v.tree, K.Key_Left)
    assert not node(v, "[]").open
    QTest.keyClick(v.tree, K.Key_Return)
    assert node(v, "[]").open


def test_ctrl_e_in_the_mapping_view_opens_the_mapping_export(qtbot, window, tmp_path) -> None:
    tab = open_tab(qtbot, window, write_json(tmp_path / "a.json", RECS))
    window.action("view_mapping").trigger()
    v = tab.active_view()
    qtbot.waitUntil(lambda: v.state.designer is not None, timeout=TIMEOUT)
    window.action("export").trigger()
    dialogs = [d for d in QApplication.topLevelWidgets() if isinstance(d, ExportDialog) and d.isVisible()]
    assert len(dialogs) == 1 and dialogs[0].mapping_active()
    dialogs[0].close()


# ----------------------------------------------------------------------------------------
# document tab: the schema view's entry point
# ----------------------------------------------------------------------------------------
def test_schema_view_entry_point_shows_the_builder(qtbot, app, tmp_path) -> None:
    session = DocSession(app, str(write_json(tmp_path / "a.json", RECS)), use_cache=False)
    tab = DocumentTab(session, view_registry=mapping_registry())
    qtbot.addWidget(tab)
    try:
        with qtbot.waitSignal(session.index_finished, timeout=TIMEOUT):
            session.start()
        schema = tab.set_active_view_kind("schema")             # pane 0
        assert isinstance(schema, SchemaStub)
        schema.mapping_requested.emit()
        assert tab.panes[0].current_kind() == "mapping" and tab.active_pane_index() == 0
        assert isinstance(tab.active_view(), MappingStub)
        # a visible pane showing the builder already: that one is used
        tab.panes[1].set_view("schema")
        tab.set_active_pane(1)
        tab.panes[1].view("schema").mapping_requested.emit()
        assert tab.panes[1].current_kind() == "schema" and tab.active_pane_index() == 0
    finally:
        tab.shutdown()
        app.tasks.wait_idle(30)


def test_real_schema_view_action(qtbot, app, tmp_path) -> None:
    from jsonreshape.gui.schemaview import SchemaView

    session = DocSession(app, str(write_json(tmp_path / "a.json", RECS)), use_cache=False)
    tab = DocumentTab(session, view_registry={"schema": SchemaView, "mapping": MappingStub})
    qtbot.addWidget(tab)
    try:
        with qtbot.waitSignal(session.index_finished, timeout=TIMEOUT):
            session.start()
        schema = tab.set_active_view_kind("schema")
        qtbot.waitUntil(lambda: schema.act_mapping.isEnabled(), timeout=TIMEOUT)
        assert schema.act_mapping.text() == "Tuzilma quruvchi…"
        assert schema.mapping_button.defaultAction() is schema.act_mapping
        assert schema.build_menu().actions()[-1] is schema.act_mapping
        schema.act_mapping.trigger()
        assert tab.active_kind() == "mapping"
    finally:
        tab.shutdown()
        app.tasks.wait_idle(30)



# ----------------------------------------------------------------------------------------
# review findings (stage 4): the export dialog's mapping mode, the window's routing
# ----------------------------------------------------------------------------------------
def test_mapping_export_refuses_gz_names_and_starts_as_json(qtbot, opened, builder, tmp_path, app) -> None:
    from jsonreshape.gui.exportdialog import STATE_KEY

    s = opened(write_json(tmp_path / "data.json", RECS))
    v = builder(s)
    app.state.set(STATE_KEY, {"fmt": "jsonl", "indent": 4})            # the last ordinary export was JSON Lines
    dlg = ExportDialog(s, v, mapping=MappingSource(tree=v.state.tree_copy))
    qtbot.addWidget(dlg)
    assert dlg.format() == "json" and dlg.output_path().endswith("data_yangi.json")
    # engine.apply writes plain JSON: a ".gz" name is refused (like the CLI), the ordinary export gzips it
    dlg.set_output_path(str(tmp_path / "out.json.gz"))
    assert dlg.error_text() == mp.t("gui.export.mapping.gzip") and not dlg.export_button.isEnabled()
    assert dlg.start_export() is None and not (tmp_path / "out.json.gz").exists()
    dlg.set_format("jsonl")
    dlg.set_output_path(str(tmp_path / "out.jsonl.gz"))
    assert mp.t("gui.export.mapping.gzip") in dlg.error_text()
    dlg.mapping_check.setChecked(False)
    assert dlg.error_text() == "" and dlg.export_button.isEnabled()
    dlg.mapping_check.setChecked(True)
    dlg.set_output_path(str(tmp_path / "out.jsonl"))
    assert dlg.error_text() == "" and dlg.export_button.isEnabled()


def test_ctrl_c_copies_the_text_selected_in_the_preview(qtbot, window, tmp_path) -> None:
    from PySide6.QtGui import QGuiApplication, QTextCursor
    from PySide6.QtTest import QTest

    window.show()
    tab = open_tab(qtbot, window, write_json(tmp_path / "a.json", RECS))
    window.action("view_mapping").trigger()
    v = tab.active_view()
    qtbot.waitUntil(lambda: v.state.designer is not None and v.preview.text() != "", timeout=TIMEOUT)
    window.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is window, timeout=TIMEOUT)
    assert v.select_node(node(v, "[].name").id)
    edit = v.preview.edit
    edit.setFocus()
    qtbot.waitUntil(edit.hasFocus, timeout=TIMEOUT)
    cursor = edit.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.Start)
    cursor.movePosition(QTextCursor.MoveOperation.Down)
    cursor.movePosition(QTextCursor.MoveOperation.EndOfLine, QTextCursor.MoveMode.KeepAnchor)
    edit.setTextCursor(cursor)
    selected = edit.textCursor().selectedText()
    assert selected.strip().startswith('"id"')
    QTest.keyClick(edit, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QGuiApplication.clipboard().text() == selected
    # without a selection in the preview (or with the focus in the tree): the row's source path
    v.tree.setFocus()
    qtbot.waitUntil(v.tree.hasFocus, timeout=TIMEOUT)
    QTest.keyClick(v.tree, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QGuiApplication.clipboard().text() == "[].name"


def test_save_as_in_another_format_opens_the_ordinary_export(qtbot, app, tmp_path, monkeypatch) -> None:
    stubs = install_stub_edit(monkeypatch, available=True)
    w = MainWindow(app, view_registry={**mapping_registry(), "mapping": MapView}, panel_factories=stub_panels())
    qtbot.addWidget(w)
    try:
        w.show()
        tab = open_tab(qtbot, w, write_json(tmp_path / "a.json", RECS))
        qtbot.waitUntil(lambda: bool(stubs) and tab.edit is stubs[-1], timeout=TIMEOUT)
        w.action("view_mapping").trigger()
        v = tab.active_view()
        qtbot.waitUntil(lambda: v.state.designer is not None, timeout=TIMEOUT)
        w._ask_yes_no = lambda title, text: True                        # "export instead"
        target = tmp_path / "plain.csv"
        assert w.save_as_current(str(target)) is False
        dialog = w.dialogs.get("export")
        assert dialog is not None and dialog.args[0] is tab.session      # the ordinary (stub) export dialog
        assert not [d for d in QApplication.topLevelWidgets() if isinstance(d, ExportDialog) and d.isVisible()]
        dialog.close()
    finally:
        w.close_without_questions()
