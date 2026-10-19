"""Round B, the shell (B1): the stage-5 tools wired into the main window, the node menu, the document tab's bars,
the export dialog and the session (docs/ARCHITECTURE.md §14.9; scratchpad ``s5/roundB_wiring.md``).

* Tools menu: compare / statistics / duplicates / JSON Schema ▸ generate + validate / repair / split / merge,
  their enabling, the statistics dock (View ▸ Panels), every tool window non-modal and one per document / file /
  application; the long jobs closing the window (or a tab: a validation) asks about.
* Node menu: compare mark / with marked, "Statistika" (the field with ``[*]``), "Shu tugunni qismlarga bo'lish…"
  with its refusals.
* The tab's bars: "Ta'mirlash…" (JSON error, broken lines), "Standart JSON'ga aylantirish…" (brace list) -> the
  repair window with the tab's hints; "Joyini ko'rsatish" -> the raw view (offset, or line for gzip).
* Export dialog: node -> CSV / XLSX / SQLite (``filetoolsdialog.node_document``), "selected records", the record-set
  scope (the duplicates' export: the view is untouched, the set closed once the dialog closed and its export
  ended), "keep the wrapper" for every format.
* ``DocSession.filter_by_condition``; ``BulkDialog.show_for(spec=, preview=)``; L6 wording; the stage-3 leftovers
  CL-4 (``confirm_drop_edits`` asks a busy clean document) and M3 (a bar's reopen commits the inline editor first).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu

from guishell_helpers import (
    TIMEOUT,
    SchemaStub,
    install_stub_edit,
    make_app,
    stub_panels,
    stub_registry,
    wait_ready,
    write_json,
)
from jsonreshape import writer
from jsonreshape.bulk import BulkSpec
from jsonreshape.gui import editing, statspanel, widgets
from jsonreshape.gui.actions import node_menu, split_node_refusal
from jsonreshape.gui.doctab import DocumentTab, confirm_drop_edits, default_view_registry
from jsonreshape.gui.exportdialog import ExportDialog, export_runner
from jsonreshape.gui.mainwindow import JOB_KINDS, MainWindow
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef
from jsonreshape.i18n import t
from jsonreshape.query import Condition, Group
from jsonreshape.recordset import RecordSet
from jsonreshape.writer import ExportOptions

pytestmark = pytest.mark.gui


# ----------------------------------------------------------------------------------------
# data
# ----------------------------------------------------------------------------------------
CITIES = ["Toshkent", "Oʻzbekiston", "Самарқанд", "O'qituvchi"]


def records(n: int = 24) -> list[dict]:
    return [{"id": i, "name": f"n{i % 5}", "city": CITIES[i % 4], "score": [1.1, 2, -0.0][i % 3],
             "orders": [{"sum": 10 + k + i % 3} for k in range(i % 3)], "tags": [f"t{i % 2}", "x"]}
            for i in range(n)]


def exact_array(path: Path, n: int = 12) -> Path:
    """An array whose records keep exact number tokens (``1.10``, ``1e400``, ``12345678901234567890``)."""
    rows = [f'{{"id": {i}, "name": "n{i % 3}", "v": {["1.10", "1e400", "12345678901234567890", "-0.0"][i % 4]}, '
            f'"items": [{{"k": {i}, "w": "Oʻz"}}, {{"k": {i + 100}, "w": "Ўз"}}]}}' for i in range(n)]
    path.write_text("[\n" + ",\n".join(rows) + "\n]\n", encoding="utf-8")
    return path


def wrapped(path: Path, n: int = 10) -> Path:
    return write_json(path, {"meta": {"version": 1, "tags": ["a"]}, "data": records(n), "total": n})


class SchemaSignals(SchemaStub):
    """The schema view's stage-5 signals (B2's contract)."""

    stats_requested = Signal(str)
    dupes_requested = Signal(str)


def registry() -> dict:
    reg = stub_registry()
    reg["schema"] = SchemaSignals
    return reg


# ----------------------------------------------------------------------------------------
# fixtures
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

    def make(reg: dict | None = None, *, real: tuple[str, ...] = ()) -> MainWindow:
        panels = stub_panels()
        for key in real:                       # these use the real dialogs
            panels.pop(key, None)
        w = MainWindow(app, view_registry=reg if reg is not None else registry(), panel_factories=panels)
        qtbot.addWidget(w)
        made.append(w)
        return w

    yield make
    for w in made:
        if not w._closing:
            w.close_without_questions()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def sessions(qtbot, app):
    made: list[DocSession] = []

    def open_file(path: Path, **kw) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
            s.start()
        return s

    yield open_file
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


def open_tab(qtbot, window: MainWindow, path: Path, **options) -> DocumentTab:
    tab = window.open_path(path, options or None)
    wait_ready(qtbot, tab.session)
    assert tab.session.state == "ready", tab.session.open_error
    return tab


def act(menu: QMenu, name: str):
    from PySide6.QtGui import QAction

    found = menu.findChild(QAction, name)
    assert found is not None, name
    return found


def held_task(app, kind: str, owner=None, *, gate: threading.Event):
    """A visible task of ``kind`` that runs until ``gate`` is set."""
    return app.tasks.run(lambda ctx: gate.wait(30), title=f"held {kind}", kind=kind,
                         owner=owner if owner is not None else object())


# ----------------------------------------------------------------------------------------
# 1. the Tools menu, its enabling and the statistics dock
# ----------------------------------------------------------------------------------------
def test_tools_menu_lists_and_enables_the_stage5_tools(qtbot, app, windows, tmp_path) -> None:
    w = windows()
    tools = w.menus["tools"]
    names = [a.objectName() if not a.isSeparator() else "-" for a in tools.actions()]
    schema_menu = w.menus["json_schema"]
    assert names == ["action_compare_files", "-", "action_stats", "action_dupes", schema_menu.menuAction().objectName(),
                     "action_repair", "-", "action_split_file", "action_merge_files", "-", "action_settings",
                     "action_open_logs", "action_environment"]
    assert [a.objectName() for a in schema_menu.actions()] == ["action_schema_generate", "action_schema_validate"]
    assert schema_menu.title() == t("gui.validatepanel.menu.json_schema")
    always = ("compare_files", "repair", "merge_files")
    with_doc = ("stats", "dupes", "schema_generate", "schema_validate", "split_file")
    assert all(w.action(k).isEnabled() for k in always)
    assert not any(w.action(k).isEnabled() for k in with_doc) and not schema_menu.isEnabled()
    open_tab(qtbot, w, write_json(tmp_path / "a.json", records()))
    assert all(w.action(k).isEnabled() for k in always + with_doc) and schema_menu.isEnabled()
    assert w.action("stats").text() == t("gui.statspanel.action")
    assert w.action("repair").toolTip() == t("gui.repairdialog.menu.repair.tip")
    # the statistics dock: a panel of DOCK_PANELS with its View ▸ Panels toggle
    assert "stats" in w.panels and w.docks["stats"].objectName() == "dock_stats"
    toggle = w.action("dock_stats")
    assert toggle in w.menus["panels"].actions() and toggle.text() == t("gui.statspanel.dock.title")
    assert w.panels["stats"].dupes_opener == w._open_dupes_for


def test_show_stats_computes_the_field_of_the_current_document(qtbot, app, windows, tmp_path) -> None:
    w = windows()
    tab = open_tab(qtbot, w, write_json(tmp_path / "s.json", records()))
    panel = w.show_stats()                                # Tools: no field -> the field box, nothing computed
    assert not w.docks["stats"].isHidden() and panel.session() is tab.session and panel.task is None
    w.show_stats(("orders", 1, "sum"), scope="all")       # a node path: indices become [*]
    assert panel.field_text() == "orders[*].sum" and panel.scope() == "all"
    qtbot.waitUntil(lambda: panel.status() == "done", timeout=TIMEOUT)
    fs = panel.result()
    assert fs.field == "orders[*].sum" and fs.present == sum(i % 3 for i in range(24))
    # the schema view's "Statistika" goes through the tab (B2's signal)
    schema = tab.set_active_view_kind("schema")
    schema.stats_requested.emit("city")
    assert panel.field_text() == "city"
    qtbot.waitUntil(lambda: panel.status() == "done" and panel.result().field == "city", timeout=TIMEOUT)
    assert panel.result().distinct == 4


def test_node_menu_statistics_and_its_refusals(qtbot, app, windows, tmp_path) -> None:
    w = windows()
    tab = open_tab(qtbot, w, wrapped(tmp_path / "w.json"))
    view = tab.active_view()
    menu = node_menu(view, tab.session, NodeRef(2, ("orders", 1, "sum")), view=view)
    stats = act(menu, "stats")
    assert stats.isEnabled() and stats.text() == t("gui.statspanel.action.node")
    stats.trigger()
    panel = w.panels["stats"]
    assert not w.docks["stats"].isHidden() and panel.field_text() == "orders[*].sum"
    qtbot.waitUntil(lambda: panel.status() == "done", timeout=TIMEOUT)
    menu.deleteLater()
    # the wrapper of a wrapped document is not a record field
    menu = node_menu(view, tab.session, NodeRef(None, ("meta",)), view=view)
    assert not act(menu, "stats").isEnabled() and act(menu, "stats").toolTip() == t("gui.actions.stats.need_record")
    menu.deleteLater()
    # a single document: its root is the one record
    single = open_tab(qtbot, w, write_json(tmp_path / "single.json", 42))
    assert single.session.doc.kind == "single"
    menu = node_menu(single.active_view(), single.session, NodeRef(None, ()))
    assert act(menu, "stats").isEnabled()
    menu.deleteLater()


def test_dupes_dialog_export_uses_the_record_set_scope(qtbot, app, windows, tmp_path) -> None:
    w = windows(real=("export",))
    path = write_json(tmp_path / "d.json", records(20))
    tab = open_tab(qtbot, w, path)
    s = tab.session
    dlg = w.show_dupes(key="name")
    assert dlg is w.show_dupes() and dlg.key_text() == "name" and not dlg.isModal()
    with qtbot.waitSignal(dlg.report_ready, timeout=TIMEOUT):
        dlg.run()
    assert dlg.report.duplicates == 15                     # 5 names: 20 - 5 kept
    dlg.export()
    qtbot.waitUntil(lambda: isinstance(w.dialogs.get("export"), ExportDialog), timeout=TIMEOUT)
    exp = w.dialogs["export"]
    assert exp.scope() == "records" and exp.scope_buttons["records"].isVisibleTo(exp)
    assert "15" in exp.scope_buttons["records"].text() and s.view_is_base       # the user's view is untouched
    records_set = exp.records
    out = tmp_path / "dupes.json"
    exp.set_output_path(out)
    with qtbot.waitSignal(exp.export_finished, timeout=TIMEOUT):
        assert exp.start_export() is not None, exp.error_text()
    kept = json.loads(out.read_text(encoding="utf-8"))
    assert [r["id"] for r in kept] == list(range(5, 20))
    exp.reject()
    qtbot.waitUntil(lambda: records_set.closed, timeout=TIMEOUT)    # released with the dialog
    dlg.reject()


def test_dupes_dialog_removal_opens_the_bulk_preview(qtbot, app, windows, tmp_path) -> None:
    w = windows(real=("bulk",))
    tab = open_tab(qtbot, w, write_json(tmp_path / "b.json", records(15)))
    qtbot.waitUntil(lambda: tab.edit is not None and tab.edit.available, timeout=TIMEOUT)
    dlg = w.show_dupes(tab.session, key="city", scope="all")
    with qtbot.waitSignal(dlg.report_ready, timeout=TIMEOUT):
        dlg.run()
    bulk = dlg.remove_duplicates()
    assert bulk is w.dialogs["bulk"] and bulk.spec().kind == "dedupe" and bulk.spec().params["key"] == "city"
    assert bulk.scope_choice() == "all"
    qtbot.waitUntil(lambda: bulk.preview_result is not None, timeout=TIMEOUT)
    assert bulk.preview_result.spec.kind == "dedupe" and bulk.preview_result.records_changed == 11
    assert not tab.dirty                                   # nothing changes before Apply
    bulk.reject()
    dlg.reject()


def test_schema_view_dupes_request_opens_the_dialog_of_that_tab(qtbot, app, windows, tmp_path) -> None:
    w = windows()
    first = open_tab(qtbot, w, write_json(tmp_path / "x.json", records(6)))
    second = open_tab(qtbot, w, write_json(tmp_path / "y.json", records(6)))
    schema = first.panes[0].set_view("schema")
    assert w.current_tab() is second
    schema.dupes_requested.emit("name")                    # from the tab that is not current
    assert w.current_tab() is first
    dlg = w.dialogs["dupes"]
    assert dlg.session is first.session and dlg.key_text() == "name"
    dlg.reject()


# ----------------------------------------------------------------------------------------
# 2. JSON Schema, repair, split / merge, compare: non-modal windows through their show_* functions
# ----------------------------------------------------------------------------------------
def test_schema_generate_and_validate_windows(qtbot, app, windows, tmp_path) -> None:
    w = windows()
    path = write_json(tmp_path / "people.json", records(10))
    tab = open_tab(qtbot, w, path)
    out = tmp_path / "people.schema.json"
    w._ask_save_path = lambda title, start, filters: str(out)       # the window's asker is used
    w.action("schema_generate").trigger()
    gen = w.dialogs["schema_generate"]
    assert gen.isVisible() and not gen.isModal() and gen.windowModality() == Qt.WindowModality.NonModal
    w.action("schema_generate").trigger()
    assert w.dialogs["schema_generate"] is gen                     # one per document
    task = gen.generate()
    assert task is not None, gen.error_text()
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    assert task.state == "done" and json.loads(out.read_text(encoding="utf-8"))["type"]
    w.action("schema_validate").trigger()
    val = w.dialogs["schema_validate"]
    assert val.isVisible() and not val.isModal() and val.session is tab.session
    val.panel.set_schema_path(out)
    assert val.panel.run_validation() is not None
    qtbot.waitUntil(lambda: not val.panel.is_running(tab.session), timeout=TIMEOUT)
    assert val.panel.issue_count_shown() == 0
    gen.close()
    w.close_tab()                                                   # the validation window closes with it
    qtbot.waitUntil(lambda: not val.isVisible(), timeout=TIMEOUT)


def test_repair_from_the_bars_and_the_tools_menu(qtbot, app, windows, tmp_path) -> None:
    w = windows()
    broken = tmp_path / "broken.json"
    broken.write_text('[{"id": 1}, {"id": 2,, "x": 3}, {"id": 4}]', encoding="utf-8")
    tab = open_tab(qtbot, w, broken, format="array")
    bar = tab.info_bar("json_error")
    assert bar is not None and t("gui.repairdialog.bar.repair") in [b.text() for b in bar.buttons()]
    with qtbot.waitSignal(tab.repair_requested, timeout=TIMEOUT) as sig:
        next(b for b in bar.buttons() if b.text() == t("gui.repairdialog.bar.repair")).click()
    assert sig.args == [str(broken), {"format": "array"}]
    dlg = w.dialogs["repair"]
    assert dlg.isVisible() and not dlg.isModal() and dlg.source_path() == str(broken)
    assert dlg.format_combo.currentData() == "array"
    assert w.show_repair(broken) is dlg                           # one window per file
    dlg.close()
    # broken JSON Lines and a brace list
    lines = tmp_path / "l.jsonl"
    lines.write_text('{"a": 1}\n{"a": \n{"a": 3}\n', encoding="utf-8")
    jl = open_tab(qtbot, w, lines)
    assert t("gui.repairdialog.bar.repair") in [b.text() for b in jl.info_bar("broken").buttons()]
    braces = tmp_path / "braces.json"
    braces.write_text('{{"a": 1}, {"a": 2}, {"a": 3}}', encoding="utf-8")
    bl = open_tab(qtbot, w, braces)
    assert bl.session.doc.kind == "brace_list"
    warn = bl.info_bar("warnings")
    button = next(b for b in warn.buttons() if b.text() == t("gui.repairdialog.bar.brace_list"))
    with qtbot.waitSignal(bl.repair_requested, timeout=TIMEOUT) as sig:
        button.click()
    assert sig.args[0] == str(braces)
    w.dialogs["repair"].close()
    # Tools ▸ repair without a tab: a window without a source
    for _ in range(3):
        w.close_tab()
    w.action("repair").trigger()
    assert w.dialogs["repair"].source_path() == "" and not w.dialogs["repair"].isModal()
    w.dialogs["repair"].close()


def test_show_repair_location_by_offset_or_line(qtbot, app, windows, tmp_path) -> None:
    import gzip

    from jsonreshape.gui.repairdialog import RepairLocation

    w = windows()
    plain = write_json(tmp_path / "p.json", records(5))
    tab = open_tab(qtbot, w, plain)
    raw = w.show_repair_location(RepairLocation(str(plain), 17, 1, 18, 3, "missing_comma"))
    assert raw is tab.showing("raw").current_view() and raw.calls[-1] == ("goto_offset", 17, 3)
    gz = tmp_path / "g.json.gz"
    gz.write_bytes(gzip.compress(plain.read_bytes()))
    gtab = open_tab(qtbot, w, gz)
    raw = w.show_repair_location(RepairLocation(str(gz), 99, 4, 2, 1, "x"))
    assert w.current_tab() is gtab and raw.calls[-1] == ("goto_line", 4)     # a prepared copy: by line
    app.statuses.clear()
    assert w.show_repair_location(RepairLocation(str(tmp_path / "other.json"), 1, 1, 1)) is None
    assert app.statuses[-1] == t("gui.repairdialog.show_location.none")


def test_split_merge_and_the_node_split(qtbot, app, windows, tmp_path) -> None:
    from jsonreshape.gui.filetoolsdialog import MergeDialog, SplitDialog

    w = windows()
    tab = open_tab(qtbot, w, exact_array(tmp_path / "e.json"))
    s = tab.session
    w.action("split_file").trigger()
    split = SplitDialog.instance(s)
    assert split is w.dialogs["split"] and split.isVisible() and not split.isModal()
    # the node menu's split: an array node is the source of the same (one per document) dialog
    view = tab.active_view()
    menu = node_menu(view, s, NodeRef(3, ("items",)), view=view, node=SimpleNamespace(kind="array", size=40))
    item = act(menu, "split_node")
    assert item.isEnabled() and item.text() == t("gui.filetools.action.split_node")
    item.trigger()
    assert SplitDialog.instance(s) is split and split.source() == "node" and split.node_ref == NodeRef(3, ("items",))
    menu.deleteLater()
    menu = node_menu(view, s, NodeRef(3, ("v",)), view=view, node=SimpleNamespace(kind="number", size=4))
    assert not act(menu, "split_node").isEnabled()
    assert act(menu, "split_node").toolTip() == t("gui.filetools.error.node_scalar", path="#3.v")
    menu.deleteLater()
    # merge: one per application, the current document first
    w.action("merge_files").trigger()
    merge = MergeDialog.instance(app)
    assert merge is w.dialogs["merge"] and not merge.isModal() and merge.inputs()[0].session is s
    merge.close()
    # the split dialog of a closed tab closes with it
    w.close_tab()
    qtbot.waitUntil(lambda: SplitDialog.instance(s) is None or not split.isVisible(), timeout=TIMEOUT)


def test_split_node_refusals(lang_uz) -> None:
    from jsonreshape.errors import UserInputError
    from jsonreshape.gui.filetoolsdialog import check_node_source

    def session(kind: str, edited: bool = False):
        return SimpleNamespace(doc=SimpleNamespace(kind=kind, edited=edited), closed=False,
                               location_text=lambda ref: "#0.a")

    ref = NodeRef(0, ("a",))
    assert split_node_refusal(session("array"), ref) is None
    assert split_node_refusal(session("array"), ref, SimpleNamespace(kind="object")) is None
    assert split_node_refusal(session("array", edited=True), ref) == t("gui.filetools.error.node_edited")
    for kind in ("jsonl", "stream", "brace_list"):
        s = session(kind)
        with pytest.raises(UserInputError) as info:
            check_node_source(s.doc)
        assert split_node_refusal(s, ref) == info.value.message and kind not in ("array",)
    assert split_node_refusal(session("array"), ref, SimpleNamespace(kind="string")) == \
        t("gui.filetools.error.node_scalar", path="#0.a")
    assert split_node_refusal(SimpleNamespace(doc=None, closed=False), ref) == t("gui.actions.no_document")


def test_compare_menu_items_and_show_node(qtbot, app, windows, tmp_path) -> None:
    from jsonreshape.gui import comparewindow

    comparewindow.clear_mark(app)
    w = windows()
    w.action("compare_files").trigger()
    files = w.dialogs["compare"]
    assert isinstance(files, comparewindow.CompareFilesDialog) and files.isVisible() and not files.isModal()
    files.reject()
    a = open_tab(qtbot, w, write_json(tmp_path / "a.json", records(6)))
    b = open_tab(qtbot, w, write_json(tmp_path / "b.json", records(7)))
    menu = node_menu(a.active_view(), a.session, NodeRef(1, ()))
    assert not act(menu, "compare_with_marked").isEnabled()
    act(menu, "compare_mark").trigger()
    menu.deleteLater()
    assert comparewindow.marked(app).ref == NodeRef(1, ())
    menu = node_menu(a.active_view(), a.session, NodeRef(1, ()))
    assert act(menu, "compare_with_marked").toolTip() == t("gui.compare.menu.with_marked.same")
    menu.deleteLater()
    menu = node_menu(b.active_view(), b.session, NodeRef(2, ()))
    with_marked = act(menu, "compare_with_marked")
    assert with_marked.isEnabled()
    with_marked.trigger()
    menu.deleteLater()
    wins = [x for x in w.findChildren(comparewindow.CompareWindow)]
    assert wins and wins[-1].isVisible()
    # "show in the tree" of a compare window: the tab comes forward, the node is selected
    assert w.current_tab() is b
    assert w.show_node(a.session, NodeRef(4, ("name",)))
    assert w.current_tab() is a and a.session.current == NodeRef(4, ("name",))
    wins[-1].reveal(b.session, NodeRef(0, ()))
    assert w.current_tab() is b and b.session.current == NodeRef(0, ())
    for win in wins:
        win.close()
    comparewindow.clear_mark(app)


# ----------------------------------------------------------------------------------------
# 3. long jobs: closing the window / a tab
# ----------------------------------------------------------------------------------------
def test_closing_the_window_asks_about_every_kind_of_long_job(qtbot, app, windows, tmp_path) -> None:
    assert {"export", "split", "merge", "repair", "validate", "schema", "compare"} <= JOB_KINDS
    w = windows()
    tab = open_tab(qtbot, w, write_json(tmp_path / "j.json", records(4)))
    gate = threading.Event()
    repair = held_task(app, "repair", gate=gate)
    generation = held_task(app, "schema", gate=gate)
    rescan = held_task(app, "schema", owner=tab.session, gate=gate)       # the schema view's scan: recomputed
    held = [repair, generation, rescan]
    assert w.running_jobs() == [repair, generation]
    asked: list[str] = []
    w._ask_yes_no = lambda title, text: asked.append(text) or False
    app.headless = False
    try:
        w.close()
        assert asked == [t("gui.main.jobs_running", count=2)] and not w._closing      # "no": stays open
    finally:
        app.headless = True
        gate.set()
    qtbot.waitUntil(lambda: all(x.state != "running" for x in held), timeout=TIMEOUT)
    assert w.running_jobs() == []


def test_closing_a_tab_asks_only_for_a_running_validation(qtbot, app, windows, tmp_path) -> None:
    w = windows()
    tab = open_tab(qtbot, w, write_json(tmp_path / "v.json", records(5)))
    gate = threading.Event()
    asked: list[str] = []
    w._ask_yes_no = lambda title, text: asked.append(title) or False
    app.headless = False
    try:
        validation = held_task(app, "validate", gate=gate)
        tab.session.keep_document_for(validation)
        split = held_task(app, "split", gate=gate)
        tab.session.keep_document_for(split)
        assert set(w.running_jobs(tab.session)) == {validation, split}
        assert w.close_tab() is False and not tab.closed and asked == [t("gui.main.jobs_stop.title")]
        w._ask_yes_no = lambda title, text: True
        app.statuses.clear()
        assert w.close_tab() is True and tab.closed
        # the split goes on with the document
        assert app.statuses[-1] == t("gui.main.jobs_continue", count=1, name="v.json")
    finally:
        app.headless = True
        gate.set()


# ----------------------------------------------------------------------------------------
# 4. the export dialog: node tables, selected records, a record set, keep the wrapper
# ----------------------------------------------------------------------------------------
def run_export(qtbot, dlg: ExportDialog):
    with qtbot.waitSignal(dlg.export_finished, timeout=TIMEOUT) as blocker:
        assert dlg.start_export() is not None, dlg.error_text()
    return blocker.args[0]


@pytest.mark.parametrize("fmt", ["csv", "xlsx", "sqlite"])
def test_node_scope_as_a_table_reopens_the_node(qtbot, app, sessions, tmp_path, fmt) -> None:
    from jsonreshape.reader import Document

    path = exact_array(tmp_path / "e.json")
    s = sessions(path)
    s.select(NodeRef(5, ("items",)))
    dlg = ExportDialog(s, scope="node")
    qtbot.addWidget(dlg)
    out = tmp_path / f"items.{fmt}"
    dlg.set_output_path(out)
    assert dlg.format() == fmt and dlg.error_text() == ""
    outcome = run_export(qtbot, dlg)
    assert outcome.scope == "node" and outcome.report.records == 2
    ref_doc = Document.open(path, record_path=(5, "items"), use_cache=False)
    try:
        ref_doc.build_index()
        want = tmp_path / f"want.{fmt}"
        writer.export(ref_doc, want, ExportOptions.from_settings(fmt, keep_wrapper=False))
    finally:
        ref_doc.close()
    if fmt == "csv":
        assert out.read_bytes() == want.read_bytes() and "Oʻz" in out.read_text(encoding="utf-8-sig")
    elif fmt == "sqlite":
        con = sqlite3.connect(str(out))
        try:
            assert [r[0] for r in con.execute('SELECT "k" FROM "records" ORDER BY rowid')] == [5, 105]
        finally:
            con.close()
    else:
        import openpyxl

        rows = list(openpyxl.load_workbook(out).active.iter_rows(values_only=True))
        assert rows[0] == ("k", "w") and [r[0] for r in rows[1:]] == [5, 105]
    dlg.close()


def test_node_table_refusals(qtbot, app, sessions, tmp_path) -> None:
    lines = tmp_path / "l.jsonl"
    lines.write_text("".join(json.dumps(r) + "\n" for r in records(4)), encoding="utf-8")
    s = sessions(lines)
    s.select(NodeRef(1, ("tags",)))
    dlg = ExportDialog(s, scope="node")
    qtbot.addWidget(dlg)
    model = dlg.format_combo.model()
    assert [model.item(i).isEnabled() for i in range(5)] == [True, True, False, False, False]
    from jsonreshape.errors import UserInputError
    from jsonreshape.gui.filetoolsdialog import check_node_source

    with pytest.raises(UserInputError) as info:
        check_node_source(s.doc)
    assert model.item(2).toolTip() == info.value.message            # the exact refusal
    dlg.set_output_path(tmp_path / "t.csv")
    assert dlg.format() == "json" and dlg.export_button.isEnabled()      # a refused format is not taken
    # a scalar node: refused by the task (its kind is not known in the GUI thread)
    a = sessions(exact_array(tmp_path / "e.json"))
    a.select(NodeRef(1, ("name",)))
    dlg2 = ExportDialog(a, scope="node")
    qtbot.addWidget(dlg2)
    dlg2.set_output_path(tmp_path / "scalar.csv")
    with qtbot.waitSignal(dlg2.export_failed, timeout=TIMEOUT) as blocker:
        dlg2.start_export()
    assert getattr(blocker.args[0], "key", "") == "gui.filetools.error.node_scalar"
    assert not (tmp_path / "scalar.csv").exists()


def test_selected_records_scope(qtbot, app, sessions, tmp_path) -> None:
    from jsonreshape.gui.bulkdialog import SelectionScope

    s = sessions(exact_array(tmp_path / "e.json"))
    chosen: list = [None]
    dlg = ExportDialog(s, scope="selected", selection=lambda: chosen[0])
    qtbot.addWidget(dlg)
    button = dlg.scope_buttons["selected"]
    assert dlg.scope() == "view" and not button.isEnabled() and button.text() == t("gui.export.scope.selected_none")
    chosen[0] = SelectionScope(positions=[1, 4, 9], gen=None)
    dlg._session_changed()                       # what activating the dialog does (changeEvent)
    assert button.isEnabled() and "3" in button.text()
    dlg.set_scope("selected")
    out = tmp_path / "sel.jsonl"
    dlg.set_output_path(out)
    outcome = run_export(qtbot, dlg)
    assert outcome.scope == "selected" and outcome.report.records == 3
    assert [json.loads(x.replace("1e400", "1"))["id"] for x in out.read_text(encoding="utf-8").splitlines()] == [1, 4, 9]
    # the rows of a filtered view (table selection)
    s.set_filter(FilterSpec("id >= 6"))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 6, timeout=TIMEOUT)
    chosen[0] = SelectionScope.for_view_rows(s, [(0, 1, 0, 2), (4, 4, 0, 0)])
    dlg._session_changed()
    dlg.set_output_path(tmp_path / "rows.json")
    run_export(qtbot, dlg)
    got = json.loads((tmp_path / "rows.json").read_text(encoding="utf-8").replace("1e400", "1"))
    assert [r["id"] for r in got] == [6, 7, 10]
    # a selection of another edit state is refused
    chosen[0] = SelectionScope(positions=[0], gen=12345)
    dlg._session_changed()
    dlg.set_output_path(tmp_path / "stale.json")
    assert dlg.start_export() is None and dlg.error_text() == t("edits.error.stale_scope")
    assert not (tmp_path / "stale.json").exists()


def test_record_set_scope_owns_and_releases_the_set(qtbot, app, sessions, tmp_path, monkeypatch) -> None:
    s = sessions(exact_array(tmp_path / "e.json"))
    rs = RecordSet.from_indices([7, 2], gen=getattr(s.doc, "gen", 0))
    dlg = ExportDialog(s, scope="records", records=rs, records_label="Takrorlar")
    qtbot.addWidget(dlg)
    assert dlg.scope() == "records" and dlg.scope_buttons["records"].text() == t(
        "gui.export.scope.records", label="Takrorlar", count="2")
    gate = threading.Event()
    real = writer.export

    def slow(doc, out, opts, source=None, *, progress=None, cancel=None):
        assert gate.wait(20)
        return real(doc, out, opts, source, progress=progress, cancel=cancel)

    monkeypatch.setattr(writer, "export", slow)
    out = tmp_path / "rs.jsonl"
    dlg.set_output_path(out)
    task = dlg.start_export()
    assert task is not None and s.view_is_base
    dlg.reject()                                  # closed while the export reads the set
    assert not rs.closed
    gate.set()
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    assert [json.loads(x.replace("1e400", "1"))["id"] for x in out.read_text(encoding="utf-8").splitlines()] == [7, 2]
    qtbot.waitUntil(lambda: rs.closed, timeout=TIMEOUT)
    # a set of another edit state is refused before anything runs
    stale = RecordSet.from_indices([0], gen=987)
    dlg2 = ExportDialog(s, scope="records", records=stale)
    qtbot.addWidget(dlg2)
    dlg2.set_output_path(tmp_path / "x.json")
    assert not dlg2.export_button.isEnabled() and t("edits.error.stale_scope") in dlg2.error_text()
    dlg2.reject()                                 # closing releases the set (no export reads it)
    assert stale.closed
    assert export_runner(app).jobs() == []


def test_keep_the_wrapper_for_every_format(qtbot, app, sessions, tmp_path) -> None:
    s = sessions(wrapped(tmp_path / "w.json"))
    dlg = ExportDialog(s, scope="all")
    qtbot.addWidget(dlg)
    for fmt in ("json", "jsonl", "csv", "xlsx", "sqlite"):
        dlg.set_format(fmt)
        assert dlg.keep_wrapper_check.isEnabled(), fmt
        assert dlg.keep_wrapper_check.toolTip() == t(f"gui.export.keep_wrapper.tip.{fmt}")
        assert dlg.export_options().keep_wrapper is True
    dlg.set_output_path(tmp_path / "keep.sqlite")
    run_export(qtbot, dlg)
    dlg.keep_wrapper_check.setChecked(False)
    assert dlg.export_options().keep_wrapper is False
    dlg.set_output_path(tmp_path / "bare.sqlite")
    run_export(qtbot, dlg)

    def tables(path: Path) -> set:
        con = sqlite3.connect(str(path))
        try:
            return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            con.close()

    assert writer.WRAPPER_TABLE in tables(tmp_path / "keep.sqlite")
    assert writer.WRAPPER_TABLE not in tables(tmp_path / "bare.sqlite")
    # JSON Lines: kept = the notice that the wrapper was not written; unchecked: no notice
    note = "writer.note.wrapper_not_written"
    dlg.set_output_path(tmp_path / "bare.jsonl")
    assert note not in run_export(qtbot, dlg).report.warning_keys
    dlg.keep_wrapper_check.setChecked(True)
    dlg.set_output_path(tmp_path / "kept.jsonl")
    assert run_export(qtbot, dlg).report.warning_keys.count(note) == 1
    # a node is the whole output: no wrapper choice
    s.select(NodeRef(None, ("meta",)))
    dlg.set_scope("node")
    assert not dlg.keep_wrapper_check.isEnabled()
    assert dlg.keep_wrapper_check.toolTip() == t("gui.export.keep_wrapper.node")


def test_export_node_wrapper_keeps_its_contract(qtbot, app, sessions, tmp_path) -> None:
    from jsonreshape.gui.exportdialog import export_node

    path = exact_array(tmp_path / "e.json")
    s = sessions(path)
    out = tmp_path / "n.json"
    done = threading.Event()
    box: dict = {}

    def work() -> None:
        try:
            box["report"] = export_node(s, NodeRef(2, ("items",)), out, "json", indent=None)
        finally:
            done.set()

    threading.Thread(target=work).start()
    assert done.wait(30)
    assert out.read_text(encoding="utf-8") == '[{"k":2,"w":"Oʻz"},{"k":102,"w":"Ўз"}]'
    assert box["report"].records == 1
    with pytest.raises(Exception) as info:
        export_node(s, NodeRef(2, ()), tmp_path / "x.csv", "csv")
    assert getattr(info.value, "key", "") == "gui.export.node_formats"
    assert not hasattr(__import__("jsonreshape.gui.exportdialog", fromlist=["x"]), "_stream_value")


# ----------------------------------------------------------------------------------------
# 5. the session's filter_by_condition, the bulk dialog's show_for(spec, preview)
# ----------------------------------------------------------------------------------------
def test_filter_by_condition(qtbot, app, sessions, tmp_path) -> None:
    s = sessions(write_json(tmp_path / "f.json", records(24)))

    def wait(n: int) -> None:
        qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == n, timeout=TIMEOUT)

    assert s.filter_by_condition(Condition("city", "eq", "Oʻzbekiston"))
    assert "Oʻzbekiston" in s.filter.text and len(Group.from_dict(s.filter.builder).items) == 1
    wait(6)                                                           # i % 4 == 1
    assert s.filter_by_condition(Condition("name", "ne", "n1"))        # the builder AND group grows
    assert s.filter.text.endswith('name != "n1"') and len(Group.from_dict(s.filter.builder).items) == 2
    wait(4)                                                           # 1 and 21 are "n1"
    s.set_filter(FilterSpec("id < 10 or id > 20"))                    # a text filter without a builder
    assert s.filter_by_condition(Condition("tags", "exists"))
    assert s.filter.text.startswith("(id < 10 or id > 20) and ") and s.filter.builder is None
    wait(13)
    s.set_filter(FilterSpec("id > `3`", lang="jmespath"))
    wait(20)
    assert s.filter_by_condition(Condition("score", "eq", 2))           # JMESPath: a quick filter of the field
    assert s.quick_filters == {"score": "score == 2"} and s.filter.lang == "jmespath"
    wait(7)                                                           # 4, 7, ..., 22
    # filter_by_value delegates
    s.reset_view()
    wait(24)
    assert s.filter_by_value(NodeRef(3, ("name",)), "n3") and s.filter.text == 'name == "n3"'
    assert not s.filter_by_value(NodeRef(None, ("x",)), 1)
    assert not hasattr(statspanel, "_and_condition")
    single = sessions(write_json(tmp_path / "one.json", 42))
    assert single.doc.kind == "single"
    assert single.filter_by_condition(Condition("a", "eq", 1)) is False and single.filter is None


def test_bulk_show_for_fills_the_form_and_previews(qtbot, app, sessions, tmp_path) -> None:
    from jsonreshape.gui.bulkdialog import BulkDialog

    s = sessions(write_json(tmp_path / "k.json", records(12)))
    qtbot.waitUntil(lambda: s.edit is not None and s.edit.available, timeout=TIMEOUT)
    spec = BulkSpec("dedupe", {"key": "name", "keep": "last"})
    dlg = BulkDialog.show_for(s, scope_default="all", spec=spec, preview=True)    # deleted when closed
    assert dlg.spec() == spec and dlg.task is not None
    again = BulkDialog.show_for(s, spec=BulkSpec("delete_field", {"path": "tags"}), preview=True)
    assert again is dlg and dlg.spec() == spec                    # a running preview is left alone
    qtbot.waitUntil(lambda: dlg.preview_result is not None, timeout=TIMEOUT)
    assert dlg.preview_result.records_changed == 7
    dlg.reject()


# ----------------------------------------------------------------------------------------
# 6. L6 wording
# ----------------------------------------------------------------------------------------
def test_l6_one_word_per_concept(lang_uz) -> None:
    assert t("gui.doctab.recovery.discard") == "O'chirish…" and "bekor" not in t("gui.main.act.discard").lower()
    assert t("gui.edit.button.cancel") == "Bekor qilish"                 # only Cancel says "Bekor qilish"
    for key in ("gui.main.act.undo.tip", "gui.main.act.redo.tip", "gui.edit.nothing_undo", "gui.edit.discarded",
                "gui.edit.nothing_to_discard", "gui.mapview.act.undo.tip", "gui.actions.edit.reason.recovery_pending"):
        assert "bekor" not in t(key).lower(), key
    assert t("gui.mapview.act.undo") == t("gui.main.act.undo").replace("&", "")
    assert t("gui.edit.button.save_as") == t("gui.main.act.save_as").replace("&", "")
    assert t("gui.edit.path.save_as") + "…" == t("gui.edit.button.save_as")
    assert t("gui.editors.type.string") == t("gui.actions.edit.type.string") == "Satr"
    assert t("gui.editors.type.null") == t("gui.actions.edit.type.null") == "null"
    after = "Ortiga"
    assert t("gui.actions.edit.insert_after").startswith(after) and t("gui.editors.insert.after") == after
    assert t("gui.tree.edit.insert.after").startswith(after) and t("gui.tree.edit.insert.record_after").startswith(after)
    for key in ("gui.tree.edit.title.value", "gui.tree.edit.title.json", "gui.tree.edit.title.key",
                "gui.tree.edit.title.insert", "gui.table.edit.title", "gui.text.edit_json.title"):
        assert t(key, path="#1.name").startswith("#1.name — "), key
    text = t("edits.error.convert", source="satr", value='"x"', target="son")
    assert text[0].isupper() and '"x"' in text
    assert t("gui.editors.type.impossible", source="satr", target="son")[0].isupper()
    for key in ("gui.doctab.recovery.found", "gui.doctab.recovery.damaged", "gui.doctab.recovery.stale"):
        assert "3 ta qadam" in t(key, time="10:00", steps="3"), key
    titles = {t("gui.edit.ask.title")} | {t(f"gui.edit.ask.title.{k}") for k in
                                           ("brace_list", "broken", "gzip", "save_as", "damaged", "target")}
    assert len(titles) == 7


def test_save_questions_have_their_own_titles(qtbot, app, sessions, tmp_path, monkeypatch) -> None:
    braces = tmp_path / "b.json"
    braces.write_text('{{"a": 1}, {"a": 2}}', encoding="utf-8")
    s = sessions(braces)
    qtbot.waitUntil(lambda: s.edit is not None and s.edit.available, timeout=TIMEOUT)
    seen: list = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: seen.append(title) or False)
    done: list = []
    s.edit.save_as(tmp_path / "copy.json", on_done=done.append)
    qtbot.waitUntil(lambda: bool(done), timeout=TIMEOUT)
    assert seen == [t("gui.edit.ask.title.brace_list")] and done == [None]     # "no": nothing written
    assert not (tmp_path / "copy.json").exists()


# ----------------------------------------------------------------------------------------
# 7. stage-3 leftovers: CL-4 and M3 on the tab's own reopen paths
# ----------------------------------------------------------------------------------------
def test_confirm_drop_edits_asks_a_busy_clean_document(qtbot, app, monkeypatch, tmp_path) -> None:
    made = install_stub_edit(monkeypatch)
    s = DocSession(app, write_json(tmp_path / "c.json", records(3)), use_cache=False)
    tab = DocumentTab(s, view_registry=stub_registry())
    qtbot.addWidget(tab)
    s.start()
    wait_ready(qtbot, s)
    qtbot.waitUntil(lambda: tab.edit is not None, timeout=TIMEOUT)
    stub = made[-1]
    answers: list = []
    confirm_drop_edits(s, tab, answers.append, why="reopen")
    assert answers == [True] and "ask_close" not in stub.names()
    stub.needs_close_answer = True                 # a long operation of an unchanged document (CL-4)
    stub.answer = "cancel"
    confirm_drop_edits(s, tab, answers.append, why="reopen")
    assert answers == [True, False] and ("ask_close", "reopen") in stub.calls
    stub.answer = "keep"
    opens = s._opens
    tab.reopen()                                    # F5 / "Qayta urinish" / the bars' reopen buttons ask too
    assert stub.calls.count(("ask_close", "reopen")) == 2
    qtbot.waitUntil(lambda: s._opens > opens, timeout=TIMEOUT)
    wait_ready(qtbot, s)
    tab.shutdown()


def tree_editor(qtbot, tab: DocumentTab, kp: tuple):
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
    QTest.keyClick(tree.grid, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: tree.inline_editor() is not None, timeout=TIMEOUT)
    return tree.inline_editor()


def test_a_bar_reopen_commits_the_inline_editor_first(qtbot, app, windows, tmp_path, monkeypatch) -> None:
    path = write_json(tmp_path / "m3.json", [{"id": i, "name": f"n{i}"} for i in range(5)])
    asked: list = []
    monkeypatch.setattr(editing, "ask_save_handler", lambda _p, name, options: asked.append(name) or "save")
    w = windows(default_view_registry())
    w.resize(1200, 700)
    w.show()
    tab = open_tab(qtbot, w, path)
    qtbot.waitUntil(lambda: tab.edit is not None and tab.edit.available and not tab.edit.busy, timeout=TIMEOUT)
    ed = tree_editor(qtbot, tab, (2, 1))
    ed.selectAll()
    QTest.keyClicks(ed, "BAR")
    assert not tab.dirty
    results: list = []
    tab.reopen(results.append)                      # what a bar's "Qayta ochish" button calls (no focus change)
    qtbot.waitUntil(lambda: json.loads(path.read_text(encoding="utf-8"))[2]["name"] == "BAR", timeout=TIMEOUT)
    assert asked == ["m3.json"]
    qtbot.waitUntil(lambda: results == [True], timeout=TIMEOUT)
    qtbot.waitUntil(lambda: tab.session.state == "ready" and tab.edit is not None and tab.edit.available,
                    timeout=TIMEOUT)
    assert not tab.dirty and app.errors == []
    # an invalid value: nothing asked, nothing reopened, the editor stays open
    ed = tree_editor(qtbot, tab, (1, 0))
    ed.setText("12abc")
    app.statuses.clear()
    opens = tab.session._opens
    tab.reopen(results.append)
    assert results[-1] is False and tab.session._opens == opens and asked == ["m3.json"]
    assert app.statuses[-1] == t("gui.edit.editor.invalid", reason=ed.error_text()) and not ed.done
    ed.cancel()


def test_record_path_dialog_commits_the_inline_editor_first(qtbot, app, windows, tmp_path, monkeypatch) -> None:
    path = wrapped(tmp_path / "rp.json", 4)
    asked: list = []
    monkeypatch.setattr(editing, "ask_save_handler", lambda _p, name, options: asked.append(name) or "save")
    w = windows(default_view_registry())
    w.resize(1200, 700)
    w.show()
    tab = open_tab(qtbot, w, path)
    qtbot.waitUntil(lambda: tab.edit is not None and tab.edit.available and not tab.edit.busy, timeout=TIMEOUT)
    tree = tab.active_view()
    kp = (0, 1)                                     # record #0, its "name"
    ed = tree_editor(qtbot, tab, kp)
    ed.selectAll()
    QTest.keyClicks(ed, "RP")
    dlg = tab.open_record_path_dialog()
    dlg.choose_row(0)                               # automatic
    dlg.accept()
    qtbot.waitUntil(lambda: json.loads(path.read_text(encoding="utf-8"))["data"][0]["name"] == "RP", timeout=TIMEOUT)
    assert asked == ["rp.json"] and tree is not None
    qtbot.waitUntil(lambda: tab.session.state == "ready", timeout=TIMEOUT)
