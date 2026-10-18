"""Regression tests of the round-2 verifier findings in the application shell and the
table view (R2-SHELL): R2-4 (a queued view export keeps its view), R2-7 (table layouts per
record path), R2-12 (concrete node paths of big rows' cells), R2-13 (the search panel's
"view busy" message goes away), R2-14 (the export dialog tells "queued" and shows a running
export when reopened), R2-15 (the filter status counts the shown search results), R2-16
(a finished export is not kept referenced), R2-17 (StatusLabel's accessible text), R2-20
(no system folder as the cache folder), R2-21 (GoTo after a cancelled indexing), R2-22
(start-up work off the GUI thread / deferred), R2-24 (F3 during a quick-find rescan)."""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import threading
import weakref
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QLabel

from guishell_helpers import TIMEOUT, make_app, records, stub_panels, stub_registry, wait_ready, write_json
from jsonreshape import core
from jsonreshape.errors import Cancelled, UserInputError
from jsonreshape.gui import qtasks, quickfind, tableview, widgets
from jsonreshape.gui import session as session_mod
from jsonreshape.gui.exportdialog import ExportDialog
from jsonreshape.gui.filterpanel import FilterPanel
from jsonreshape.gui.mainwindow import MainWindow
from jsonreshape.gui.searchpanel import SearchPanel
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef
from jsonreshape.gui.settingsdialog import SettingsDialog, cache_dir_problem
from jsonreshape.gui.statusbar import StatusLabel
from jsonreshape.gui.tableview import TableView, cell_data, column_spec, huge_cells, load_row
from jsonreshape.reader import Document
from jsonreshape.recordset import RecordSet

pytestmark = pytest.mark.gui

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------------
@pytest.fixture
def app(qapp, tmp_path, lang_uz):
    ctx = make_app(tmp_path)
    ctx.tasks.set_poll_interval(10)
    yield ctx
    for w in list(widgets._kept_alive):
        w.close()
    ctx.tasks.cancel_all()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def opened(qtbot, app):
    made: list[DocSession] = []

    def factory(path, **kw) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        s.start()
        wait_ready(qtbot, s)
        assert s.state == "ready", s.open_error
        return s

    yield factory
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def fast_retire(monkeypatch):
    """Replaced record sets are closed 0.2 s later (instead of 5 s)."""
    monkeypatch.setattr(DocSession, "RETIRE_DELAY_MS", 200)


@pytest.fixture
def blockers(app):
    """``block()`` fills every heavy-task slot until the returned event is set (exports
    then wait in the queue)."""
    gates: list[threading.Event] = []

    def block() -> threading.Event:
        gate = threading.Event()
        gates.append(gate)

        def heavy(ctx):
            while not gate.wait(0.01):
                ctx.check()
            return 1

        for k in range(qtasks.MAX_HEAVY_TASKS):
            app.tasks.run(heavy, title=f"blocker {k}", kind="schema", heavy=True)
        return gate

    yield block
    for gate in gates:
        gate.set()


def flush_deletes() -> None:
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def pump(qtbot, ms: int) -> None:
    qtbot.wait(ms)


def start_export(session: DocSession, out: Path, scope: str = "view") -> tuple[ExportDialog, object]:
    dlg = ExportDialog(session)
    dlg.set_output_path(str(out))
    dlg.set_scope(scope)
    task = dlg.start_export()
    assert task is not None, dlg.error_text()
    return dlg, task


def json_len(path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    return len(data)


# ========================================================================================
# R2-4 / R2-14 / R2-16: queued exports of the view
# ========================================================================================
@pytest.mark.parametrize("change", ["clear_filter", "reset_view", "show_records"])
def test_queued_view_export_keeps_its_view(qtbot, app, opened, tmp_path, fast_retire, blockers, change) -> None:
    """R2-4: the view is leased when the export is started -- not when its thread runs --
    so replacing the view while the export waits in the heavy-task queue (the old record
    set retired and its delay long over) does not fail the export; R2-14: the dialog says
    the export is queued, then that it runs."""
    s = opened(write_json(tmp_path / "a.json", records(300)))
    s.set_filter(FilterSpec("id < 100"))
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    old_view = s.view_recordset()
    gate = blockers()
    out = tmp_path / "e.json"
    dlg, task = start_export(s, out)
    qtbot.addWidget(dlg)
    assert task.queued and dlg.queued
    assert dlg.status_label.full_text() == t_uz("gui.export.status.queued", path=str(out))
    assert "Navbatda" in dlg.status_label.full_text()
    assert s.held_sets() == [old_view]
    if change == "clear_filter":
        s.set_filter(None)
    elif change == "reset_view":
        s.reset_view()
    else:
        s.show_records(RecordSet.from_indices([1, 2, 3]), "Qidiruv natijasi")
    assert not s.view_busy and s.view_recordset() is not old_view
    pump(qtbot, 700)                        # 3.5x the retire delay: still open (leased)
    assert not old_view.closed and task.state == "running"
    gate.set()
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    assert task.state == "done", (task.error, app.errors)
    assert json_len(out) == 100 and not app.errors
    assert "Yozilmoqda" in dlg.status_label.full_text() or "Tayyor" in dlg.status_label.full_text()
    # the lease ends with the task: the old set is closed by the next retire tick
    qtbot.waitUntil(lambda: old_view.closed, timeout=TIMEOUT)
    assert s.held_sets() == [] and s._doc_users == []


def test_queued_view_export_survives_reopen_and_close(qtbot, app, opened, tmp_path, fast_retire, blockers) -> None:
    """R2-4: the leased view outlives a reopen (F5) and the tab closing while the export
    waits for its turn."""
    s = opened(write_json(tmp_path / "a.json", records(300)))
    s.set_filter(FilterSpec("id < 100"))
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    view = s.view_recordset()
    gate = blockers()
    out = tmp_path / "e.json"
    dlg, task = start_export(s, out)
    qtbot.addWidget(dlg)
    s.set_filter(None)                      # retired ...
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    s.reopen()                              # ... then the document is reopened ...
    wait_ready(qtbot, s)
    pump(qtbot, 500)
    s.close()                               # ... and the tab closed
    flush_deletes()
    pump(qtbot, 300)
    assert not view.closed
    gate.set()
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    assert task.state == "done", task.error
    assert json_len(out) == 100
    qtbot.waitUntil(lambda: view.closed, timeout=TIMEOUT)


def test_reopened_export_dialog_shows_the_running_export(qtbot, app, opened, tmp_path, blockers) -> None:
    """R2-14: a dialog opened while an export of the file runs (here: queued) shows it --
    progress bar, "queued" text, stop button -- and still lets another export start."""
    s = opened(write_json(tmp_path / "a.json", records(50)))
    gate = blockers()
    first, task = start_export(s, tmp_path / "one.json", "all")
    qtbot.addWidget(first)
    first.reject()
    again = ExportDialog(s)
    qtbot.addWidget(again)
    assert again.job is first.job and again.task is task and again.running and again.queued
    assert again.progress_bar.isVisibleTo(again) and again.cancel_task_button.isVisibleTo(again)
    assert "Navbatda" in again.status_label.full_text() and "one.json" in again.status_label.full_text()
    assert again.export_button.isEnabled()                  # another export can still be started
    gate.set()
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    qtbot.waitUntil(lambda: again.outcome is not None, timeout=TIMEOUT)
    assert "Tayyor" in again.status_label.full_text() and not again.running
    assert not again.cancel_task_button.isVisibleTo(again)
    # stop from the reopened dialog
    gate2 = blockers()
    second, task2 = start_export(s, tmp_path / "two.json", "all")
    qtbot.addWidget(second)
    third = ExportDialog(s)
    qtbot.addWidget(third)
    assert third.task is task2
    third.cancel_export()
    qtbot.waitUntil(lambda: task2.state == "cancelled", timeout=TIMEOUT)
    assert "To'xtatildi" in third.status_label.full_text() and not third.running
    gate2.set()
    assert not (tmp_path / "two.json").exists()


def test_finished_export_is_not_kept_referenced(qtbot, app, tmp_path) -> None:
    """R2-16: once an export ended, the session forgets its task (no job / dialog /
    outcome stays referenced until the next export or the tab closes) and the window
    forgets the closed export dialog."""
    app.tasks.set_poll_interval(10)
    window = MainWindow(app, view_registry=stub_registry(), panel_factories=dict(stub_panels(), export=ExportDialog))
    qtbot.addWidget(window)
    tab = window.open_path(write_json(tmp_path / "a.json", records(40)))
    wait_ready(qtbot, tab.session)
    s = tab.session
    dlg = window.show_export()
    assert isinstance(dlg, ExportDialog) and window.dialogs["export"] is dlg
    dlg.set_output_path(str(tmp_path / "e.json"))
    task = dlg.start_export()
    assert task is not None and s._doc_users == [task]
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)
    assert s._doc_users == [] and s.held_sets() == []
    job_ref = weakref.ref(dlg.job)
    outcome_ref = weakref.ref(task.result)
    dlg_ref = weakref.ref(dlg)
    dlg.reject()                                  # WA_DeleteOnClose
    del dlg, task
    for report in list(widgets._kept_alive):      # the report window
        report.close()
    del report
    flush_deletes()
    assert "export" not in window.dialogs
    gc.collect()
    assert dlg_ref() is None and job_ref() is None and outcome_ref() is None
    window.close()


# ========================================================================================
# R2-13: the search panel's "view busy" message
# ========================================================================================
def test_search_view_busy_message_goes_away(qtbot, app, opened, tmp_path, monkeypatch) -> None:
    gate = threading.Event()
    real = session_mod.filter_records

    def gated(*args, **kwargs):
        cancel = kwargs.get("cancel")
        while not gate.wait(0.01):
            if cancel is not None:
                cancel.check()
        return real(*args, **kwargs)

    monkeypatch.setattr(session_mod, "filter_records", gated)
    s = opened(write_json(tmp_path / "a.json", records(100)))
    panel = SearchPanel(app)
    qtbot.addWidget(panel)
    panel.set_session(s)
    panel.set_query_text("n1")
    s.set_filter(FilterSpec("id < 50"))
    assert s.view_busy
    assert panel.run_search() is None
    busy = t_uz("gui.search.view_busy")
    assert panel.error_text() == busy
    gate.set()
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: panel.error_text() == "", timeout=TIMEOUT)
    assert panel.run_search() is not None                 # and searching works now
    # an input error shown meanwhile stays (only the busy message is cleared)
    gate.clear()
    s.set_filter(FilterSpec("id < 20"))
    assert panel.run_search() is None and panel.error_text() == busy
    panel.path_edit.setText("a[")                         # an invalid path replaces the message
    assert panel.error_text() and panel.error_text() != busy
    gate.set()
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    pump(qtbot, 50)
    assert panel.error_text() and panel.error_text() != busy
    panel.shutdown()


# ========================================================================================
# R2-15: the filter status counts the shown search results
# ========================================================================================
def test_filter_status_counts_the_shown_records(qtbot, app, opened, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", records(300)))
    panel = FilterPanel(app)
    qtbot.addWidget(panel)
    panel.set_session(s)
    assert panel.status_text() == t_uz("gui.filter.status.none", total="300")
    s.show_records(RecordSet.from_indices(range(0, 300, 3)), "Qidiruv natijasi: n1")
    assert panel.status_text() == t_uz("gui.filter.status.none_shown", total="100", source="Qidiruv natijasi: n1")
    s.set_filter(FilterSpec("id < 30"))
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert s.view_len() == 10
    assert panel.status_text().startswith(t_uz("gui.filter.status.result_shown", count="10", total="100",
                                                source="Qidiruv natijasi: n1"))
    s.reset_view()
    assert panel.status_text() == t_uz("gui.filter.status.none", total="300")


# ========================================================================================
# R2-17: StatusLabel's accessible text
# ========================================================================================
@pytest.mark.parametrize("fixed", [True, False])
def test_status_label_accessible_text_is_the_shown_text(qtbot, fixed) -> None:
    label = StatusLabel(fixed=fixed)
    qtbot.addWidget(label)
    iface = QAccessible.queryAccessibleInterface(label)
    assert iface.text(QAccessible.Text.Name).strip() == ""         # never the sizing "X"
    label.setText("1 234 567 yozuv")
    sizing = QLabel.text(label)
    for text in ("1 234 568 yozuv", "1 240 000 yozuv"):
        label.setText(text)
        assert iface.text(QAccessible.Text.Name) == text == label.accessibleName()
    if fixed:
        assert QLabel.text(label) == sizing != label.full_text()   # the Qt text stays the sizing text
    label.setText("")
    assert iface.text(QAccessible.Text.Name).strip() == ""


# ========================================================================================
# R2-20: no system folder / drive root as the cache folder
# ========================================================================================
@pytest.mark.skipif(sys.platform != "win32", reason="Windows folders")
def test_cache_folder_refuses_system_folders(monkeypatch, tmp_path, lang_uz) -> None:
    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    monkeypatch.setenv("WINDIR", "C:\\WINDOWS")
    monkeypatch.setenv("ProgramFiles", "C:\\Program Files")
    monkeypatch.setenv("ProgramFiles(x86)", "C:\\Program Files (x86)")
    monkeypatch.setenv("ProgramData", "C:\\ProgramData")
    for bad in ("C:\\Windows", "c:\\windows\\", "C:\\Windows\\Temp\\kesh", "C:/Windows/System32",
                "\\\\?\\C:\\Windows\\tmp", "C:\\Program Files", "C:\\Program Files\\App\\kesh",
                "C:\\Program Files (x86)\\x", "C:\\ProgramData"):
        problem = cache_dir_problem(bad)
        assert problem is not None and "tizim papkasi" in problem, bad
    for root in ("C:\\", "c:/", "\\\\server\\share", "\\\\server\\share\\"):
        problem = cache_dir_problem(root)
        assert problem is not None and "ildiz" in problem, root
    # allowed: a folder of one's own (also inside ProgramData), names that only start alike
    for good in (str(tmp_path / "kesh"), "C:\\ProgramData\\jsonreshape", "C:\\Windows2\\kesh",
                 "C:\\Program Files Data\\kesh"):
        assert cache_dir_problem(good) is None, good


@pytest.mark.skipif(sys.platform != "win32", reason="Windows folders")
def test_settings_dialog_refuses_the_windows_folder(qtbot, app, monkeypatch, lang_uz) -> None:
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    dlg = SettingsDialog(app)
    qtbot.addWidget(dlg)
    dlg.cache_dir_edit.setText("C:\\Windows")
    errors = dlg.errors()
    assert "cache_dir" in errors and "tizim papkasi" in errors["cache_dir"]
    assert not dlg.save_button.isEnabled()
    with pytest.raises(UserInputError):
        dlg.settings()
    dlg.cache_dir_edit.setText("C:\\")
    assert "ildiz" in dlg.errors()["cache_dir"]
    dlg.cache_dir_edit.setText("")
    assert "cache_dir" not in dlg.errors()
    dlg.reject()


# ========================================================================================
# R2-21: GoTo after the indexing was cancelled
# ========================================================================================
def test_goto_after_a_cancelled_indexing_does_not_say_still_indexing(qtbot, app, tmp_path, monkeypatch) -> None:
    def cancelled(self, *args, **kwargs):
        raise Cancelled()

    monkeypatch.setattr(Document, "build_index", cancelled)
    s = DocSession(app, write_json(tmp_path / "a.json", records(20)), use_cache=False)
    s.start()
    qtbot.waitUntil(lambda: s.state == "failed", timeout=TIMEOUT)
    assert isinstance(s.open_error, Cancelled) and s.doc is not None and not s.doc.index_complete
    with pytest.raises(UserInputError) as info:
        s.node(NodeRef(10_000, ()))
    assert info.value.key == "gui.session.record_not_found_stopped"
    assert "to'xtatilgan" in info.value.message and "hali indekslanmoqda" not in info.value.message
    s.close()
    assert app.tasks.wait_idle(30)


# ========================================================================================
# R2-7: table layouts per record path
# ========================================================================================
def two_paths(tmp_path: Path) -> Path:
    return write_json(tmp_path / "two.json", {"a": [{"x": i, "xx": i} for i in range(5)],
                                             "b": [{"y": i, "yy": -i} for i in range(7)]})


@pytest.fixture
def tables(qtbot):
    made: list[TableView] = []

    def make(session: DocSession) -> TableView:
        view = TableView(session)
        qtbot.addWidget(view)
        view.resize(900, 500)
        view.show()
        view.activate()
        made.append(view)
        return view

    yield make
    for v in made:
        v.shutdown()


def test_layouts_are_kept_per_record_path(qtbot, app, opened, tables, tmp_path) -> None:
    path = two_paths(tmp_path)
    s = opened(path, record_path="a")
    tb = tables(s)
    qtbot.waitUntil(lambda: tb.column_paths() == ["x", "xx"], timeout=TIMEOUT)
    tb.set_columns(["xx", "$index"])
    tb.grid.set_column_width(0, 222)
    tb.save_layout()
    stored = app.state.table_layout(path)
    assert stored["kind"] == "wrapped" and stored["record_path"] == ["a"] and "others" not in stored
    # another record path in the same tab: its own layout (none yet: its defaults)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(record_path="b")
    tb.set_columns(["y"])
    tb.save_layout()
    stored = app.state.table_layout(path)
    assert stored["record_path"] == ["b"] and [o["record_path"] for o in stored["others"]] == [["a"]]
    s.close()
    # new sessions get the layout of their records
    sa = opened(path, record_path="a")
    ta = tables(sa)
    assert ta.column_paths() == ["xx", "$index"] and ta._columns_source == "layout"
    assert ta.grid.columns()[0].width == 222
    sb = opened(path, record_path="b")
    tb2 = tables(sb)
    assert tb2.column_paths() == ["y"] and tb2._columns_source == "layout"
    # forgetting one record path's layout keeps the other one
    tb2.reset_columns()
    stored = app.state.table_layout(path)
    assert stored["record_path"] == ["a"] and "others" not in stored
    qtbot.waitUntil(lambda: tb2.column_paths() == ["y", "yy"], timeout=TIMEOUT)


def test_layout_of_another_record_path_is_not_used_on_automatic_open(qtbot, app, opened, tables, tmp_path) -> None:
    """The verifier's flow: automatic record path (``a``), the user switches to ``b`` and
    widens a column (saved for ``b``); the file opened again automatically shows ``a``'s
    columns -- not ``b``'s with every cell empty."""
    path = two_paths(tmp_path)
    s = opened(path)
    tb = tables(s)
    qtbot.waitUntil(lambda: tb._columns_source == "default", timeout=TIMEOUT)
    auto_cols = tb.column_paths()
    other = "b" if "x" in auto_cols else "a"
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(record_path=other)
    qtbot.waitUntil(lambda: tb._columns_source == "default" and tb.column_paths() != auto_cols, timeout=TIMEOUT)
    tb.grid.set_column_width(0, 250)
    tb.save_layout()
    s.close()
    s2 = opened(path)
    tb2 = tables(s2)
    qtbot.waitUntil(lambda: tb2.column_paths() == auto_cols, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: tb2.cell_text(0, auto_cols[0]) == "0", timeout=TIMEOUT)


def test_old_layouts_are_read_for_the_automatic_record_path_only(qtbot, app, opened, tables, tmp_path) -> None:
    """A layout saved before layouts were kept per record path (version 1, no "kind") is
    used when the file is opened automatically, not for an explicitly chosen record path."""
    path = two_paths(tmp_path)
    app.state.set_table_layout(path, {"version": 1, "columns": [{"path": "$index", "width": 140, "hidden": False}],
                                      "frozen": 0, "filter_row": True})
    s = opened(path)
    tb = tables(s)
    assert tb.column_paths() == ["$index"] and tb._columns_source == "layout"
    other = "a" if list(s.doc.record_path) == ["b"] else "b"     # not the automatic choice
    s.close()
    so = opened(path, record_path=other)
    tbo = tables(so)
    qtbot.waitUntil(lambda: tbo._columns_source == "default", timeout=TIMEOUT)
    assert "$index" not in tbo.column_paths()
    # saving that layout keeps the old entry for the automatic opens
    tbo.grid.set_column_width(0, 300)
    tbo.save_layout()
    stored = app.state.table_layout(path)
    assert stored["record_path"] == [other] and stored["others"][0]["columns"][0]["path"] == "$index"
    s3 = opened(path)
    tb3 = tables(s3)
    assert tb3.column_paths() == ["$index"]


def test_layout_variants_are_capped(qtbot, app, opened, tables, tmp_path) -> None:
    value = {f"k{i}": [{"f": i}] for i in range(tableview.LAYOUT_VARIANTS_MAX + 3)}
    path = write_json(tmp_path / "many.json", value)
    for i in range(tableview.LAYOUT_VARIANTS_MAX + 3):
        s = opened(path, record_path=f"k{i}")
        tb = tables(s)
        tb.set_columns(["f", "$index"])
        tb.save_layout()
        s.close()
    stored = app.state.table_layout(path)
    variants = [stored] + stored["others"]
    assert len(variants) == tableview.LAYOUT_VARIANTS_MAX
    assert [v["record_path"] for v in variants][:2] == [[f"k{tableview.LAYOUT_VARIANTS_MAX + 2}"],
                                                         [f"k{tableview.LAYOUT_VARIANTS_MAX + 1}"]]


# ========================================================================================
# R2-12: concrete node paths in the cells of big rows
# ========================================================================================
BIG_COLUMNS = ["id", "w[*].x", "w[*]", "w[2].x", "obj.*", "obj.*.c", "*", "arr[*]", "one[*].k", "deep[*][*].v",
               "m.*.n", "missing[*]", "$"]


def _big_records() -> list:
    return [
        {"id": 0, "pad": "p" * 3000, "w": [{"x": 1}, {"y": 2}, {"x": {"z": 3}}], "obj": {"a": {"c": 1}, "b": 2},
         "arr": [5], "one": [{"q": 1}, {"q": 2}, {"k": "only"}], "deep": [[{"v": 1}], [{"u": 0}, {"v": 2}]],
         "m": {"p": {"n": 1}}},
        {"id": 1, "pad": "q" * 3000, "w": [{"y": 0}, {"x": "solo"}], "obj": {"z": {"c": [1, 2]}}, "arr": [],
         "one": [], "deep": [[], [{"u": 1}, {"u": 2}, {"v": {"k": 1}}]], "m": {"r": {"n": 2}, "s": {"n": 3}}},
    ]


def _check_paths(rows, parsed, specs) -> None:
    for i, (row, value) in enumerate(zip(rows, parsed)):
        assert row.streamed
        for spec in specs:
            want = cell_data(value, spec, i)
            got = row.cells[spec.text]
            assert got.count == want.count, (i, spec.text)
            assert tuple(got.path) == tuple(want.path), (i, spec.text, got.path, want.path)


@pytest.mark.skipif(core.BACKEND != "rust", reason="the extracted path needs the Rust core")
@pytest.mark.parametrize("keyed", [False, True])
def test_extracted_cells_have_the_concrete_node_path(tmp_path, monkeypatch, keyed) -> None:
    monkeypatch.setattr(tableview, "STREAM_RECORD_BYTES", 1024)
    recs = _big_records()
    value = {f"r{i}": r for i, r in enumerate(recs)} if keyed else recs
    path = write_json(tmp_path / "big.json", value)
    specs = tuple(column_spec(c) for c in BIG_COLUMNS)
    with Document.open(path, use_cache=False) as doc:
        doc.build_index()
        assert bool(doc.member_records) == keyed
        rows = [load_row(doc, i, specs, 0) for i in range(doc.record_count)]
    assert all(not r.huge for r in rows)
    _check_paths(rows, recs, specs)
    assert rows[0].cells["one[*].k"].path == ("one", 2, "k")          # not the column prefix ("one",)
    assert rows[1].cells["w[*].x"].path == ("w", 1, "x")
    assert rows[1].cells["deep[*][*].v"].path == ("deep", 1, 2, "v")


@pytest.mark.parametrize("keyed", [False, True])
def test_streamed_cells_have_the_concrete_node_path(tmp_path, keyed) -> None:
    recs = _big_records()
    value = {f"r{i}": r for i, r in enumerate(recs)} if keyed else recs
    path = write_json(tmp_path / "big.json", value)
    specs = tuple(column_spec(c) for c in BIG_COLUMNS)
    rows = []
    with Document.open(path, use_cache=False) as doc:
        doc.build_index()
        for i in range(doc.record_count):
            key = doc.record_key(i) if doc.member_records else None
            cells = huge_cells(doc, i, doc.record_span(i), bool(doc.member_records), key, specs, None, None)
            rows.append(tableview._Row(i, key, tableview._NOVALUE, True, cells, 0, 0))
    _check_paths(rows, recs, specs)
    assert rows[0].cells["one[*].k"].path == ("one", 2, "k")
    assert rows[1].cells["m.*.n"].path == ("m",)


@pytest.mark.skipif(core.BACKEND != "rust", reason="the extracted path needs the Rust core")
def test_selecting_a_big_rows_cell_selects_its_node(qtbot, app, opened, tables, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tableview, "STREAM_RECORD_BYTES", 1024)
    s = opened(write_json(tmp_path / "big.json", _big_records()))
    tb = tables(s)
    tb.set_columns(["id", "one[*].k", "w[*].x"])
    qtbot.waitUntil(lambda: tb.cell_text(1, "w[*].x") == "solo", timeout=TIMEOUT)
    assert tb._row(1).streamed
    col = [c.id for c in tb.grid.columns()].index("w[*].x")
    assert tb._ref_at(1, col, require_loaded=True) == NodeRef(1, ("w", 1, "x"))
    col = [c.id for c in tb.grid.columns()].index("one[*].k")
    assert tb._ref_at(0, col, require_loaded=True) == NodeRef(0, ("one", 2, "k"))


# ========================================================================================
# R2-24: F3 while a quick-find rescan runs
# ========================================================================================
def test_f3_during_a_quick_find_rescan_is_not_dropped(qtbot, app, opened, tables, tmp_path, monkeypatch) -> None:
    s = opened(write_json(tmp_path / "p.json", [{"id": i, "city": "Samarqand" if i % 3 == 1 else "Buxoro"}
                                                for i in range(30)]))
    tb = tables(s)
    tb.set_columns(["id", "city"])
    qtbot.waitUntil(lambda: all(tb.cell_text(r, "city") for r in range(20)), timeout=TIMEOUT)
    tb.grid.set_current(0, 0)
    tb.quick_find()
    tb.find_bar._edit.setText("samar")
    tb.find_bar.flush()
    qtbot.waitUntil(lambda: tb._find_task is None and bool(tb.quick_find_matches()), timeout=TIMEOUT)
    assert tb.grid.current()[0] == 1
    # a silent rescan (rows loaded / scrolled) that takes a while
    gate = threading.Event()
    real = quickfind.make_matcher

    def slow_matcher(text, case_sensitive):
        match = real(text, case_sensitive)

        def matcher(cell_text):
            gate.wait(10)
            return match(cell_text)
        return matcher

    monkeypatch.setattr(quickfind, "make_matcher", slow_matcher)
    tb._find_rescan()
    assert tb._find_task is not None
    tb.find_next()                       # F3 twice and Shift+F3 once while it runs
    tb.find_next()
    tb.find_next(backwards=True)
    assert tb.grid.current()[0] == 1     # nothing moved yet ...
    gate.set()
    qtbot.waitUntil(lambda: tb._find_task is None, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: tb.grid.current()[0] == 4, timeout=TIMEOUT)   # ... then 1 -> 4 -> 7 -> 4
    assert tb._find_queue == []
    # after rows moved (hits dropped, a rescan due), F3 runs the rescan now and steps then
    monkeypatch.setattr(quickfind, "make_matcher", real)
    tb._find_reset()
    assert not tb._find_order and tb._find_timer.isActive()
    tb.find_next()
    assert not tb._find_timer.isActive()
    qtbot.waitUntil(lambda: tb._find_task is None and tb.grid.current()[0] == 7, timeout=TIMEOUT)


# ========================================================================================
# R2-22: start-up work deferred / off the GUI thread
# ========================================================================================
def test_startup_imports_only_the_default_views_and_psutil_off_the_gui_thread(tmp_path) -> None:
    """The view modules a new tab does not show are imported when first shown, and the
    status bar imports psutil in a silent task (both were GUI-thread start-up work)."""
    code = (
        "import sys, threading, importlib.abc\n"
        "seen = {}\n"
        "class Spy(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name in ('psutil', 'jsonreshape.gui.rawview', 'jsonreshape.gui.schemaview'):\n"
        "            seen.setdefault(name, threading.current_thread().name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Spy())\n"
        "from PySide6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "from jsonreshape.gui.context import AppContext\n"
        "from jsonreshape.gui.guistate import GuiState\n"
        "from jsonreshape.gui.mainwindow import MainWindow\n"
        "from jsonreshape.gui import doctab\n"
        f"ctx = AppContext(state=GuiState.load({str(tmp_path / 'state.json')!r}), headless=True)\n"
        "w = MainWindow(ctx)\n"
        "main_thread = threading.current_thread().name\n"
        "reg = w._registry\n"
        "lazy = sorted(k for k, v in reg.items() if isinstance(v, doctab.LazyView))\n"
        "loaded = [m for m in ('jsonreshape.gui.rawview', 'jsonreshape.gui.schemaview') if m in sys.modules]\n"
        "ctx.tasks.wait_idle(20)\n"
        "print('LAZY', lazy)\n"
        "print('LOADED', loaded)\n"
        "print('PSUTIL', seen.get('psutil') != main_thread, w.status._process is not None)\n"
        "view = reg['raw'].resolve()\n"
        "print('RESOLVED', view.__name__, 'jsonreshape.gui.rawview' in sys.modules)\n"
        "w.close()\n"
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", JSONRESHAPE_HOME=str(tmp_path / "home"))
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "LAZY ['raw', 'schema', 'table']" in out, out
    assert "LOADED []" in out, out
    assert "PSUTIL True True" in out, out
    assert "RESOLVED RawView True" in out, out


# ----------------------------------------------------------------------------------------
def t_uz(key: str, **params) -> str:
    from jsonreshape.i18n import t

    return t(key, **params)
