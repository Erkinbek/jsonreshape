"""Regression tests of the stage-2 review fixes in the application shell (FX5): session,
context, GUI state, search panel, export dialog, document tab, main window, navigation,
status bar and tasks panel. Each test names the review finding it covers."""

from __future__ import annotations

import ast
import gc
import json
import os
import subprocess
import threading
import time
import weakref
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QMimeData, QObject, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QLabel

from guishell_helpers import TIMEOUT, make_app, records, stub_panels, stub_registry, wait_ready, wrapped_file, write_json
from jsonreshape import search as search_mod
from jsonreshape import writer
from jsonreshape.errors import UserInputError
from jsonreshape.gui import widgets
from jsonreshape.gui.doctab import DocumentTab
from jsonreshape.gui.exportdialog import ExportDialog, export_node, export_runner
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.mainwindow import MainWindow
from jsonreshape.gui.navigation import BrokenLinesDialog
from jsonreshape.gui.searchpanel import SearchPanel
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef, find_position, find_positions
from jsonreshape.gui.statusbar import StatusBar, StatusLabel
from jsonreshape.gui.taskspanel import bar_value, eta_text, progress_texts, title_text
from jsonreshape.i18n import t
from jsonreshape.recordset import RecordSet
from jsonreshape.sortkeys import SortKey
from jsonreshape.tasks import ProgressInfo, format_bytes, format_duration

pytestmark = pytest.mark.gui

PKG = Path(__file__).resolve().parents[1] / "jsonreshape"
FX5_MODULES = ("session", "context", "guistate", "searchpanel", "exportdialog", "doctab", "mainwindow",
               "navigation", "statusbar", "taskspanel")


# ----------------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------------
@pytest.fixture
def app(qapp, tmp_path, lang_uz):
    ctx = make_app(tmp_path)
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
def window(qtbot, app):
    w = MainWindow(app, view_registry=stub_registry(), panel_factories=stub_panels())
    qtbot.addWidget(w)
    yield w
    if not w._closing:
        w.close()


def flush_deletes() -> None:
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()


def people(path: Path, n: int = 300) -> Path:
    return write_json(path, [{"id": i, "name": ("foo" if i % 10 == 0 else "bar") + str(i), "grp": i % 3,
                              "city": "Toshkent" if i % 7 == 0 else "Samarqand"} for i in range(n)])


def slow_export(monkeypatch, gate: threading.Event) -> None:
    real = writer.export

    def slow(doc, out, opts, source=None, *, progress=None, cancel=None):
        while not gate.wait(0.02):
            if cancel is not None:
                cancel.check()
        return real(doc, out, opts, source, progress=progress, cancel=cancel)

    monkeypatch.setattr(writer, "export", slow)


# ----------------------------------------------------------------------------------------
# T1 / T2: no Python layout code on the layout paths of the shell
# ----------------------------------------------------------------------------------------
def test_no_python_layout_overrides_in_the_shell_modules() -> None:
    """T1 (crash: a worker's GC ran while the GUI thread was inside a Python QLayout
    subclass) and T2 (GIL convoy through Python size hints): none of the shell modules
    defines a QLayout subclass or overrides a size-hint / layout method in Python."""
    banned = {"sizeHint", "minimumSizeHint", "minimumSize", "maximumSize", "heightForWidth", "hasHeightForWidth",
              "setGeometry", "expandingDirections"}
    problems = []
    for name in FX5_MODULES:
        tree = ast.parse((PKG / "gui" / f"{name}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = {getattr(b, "id", getattr(b, "attr", "")) for b in node.bases}
                if bases & {"QLayout", "QBoxLayout", "QHBoxLayout", "QVBoxLayout", "QGridLayout", "QFormLayout"}:
                    problems.append(f"{name}.{node.name}: Python layout class")
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name in banned:
                        problems.append(f"{name}.{node.name}.{item.name}")
    assert not problems, problems


def test_search_panel_rows_are_standard_layouts(qtbot, app) -> None:
    panel = SearchPanel(app)
    qtbot.addWidget(panel)
    from jsonreshape.gui import searchpanel

    assert not hasattr(searchpanel, "_FlowLayout")
    for layout in panel.findChildren(QObject):
        if layout.inherits("QLayout"):
            assert type(layout).__module__.startswith("PySide6"), type(layout)
    panel.resize(300, 400)
    panel.show()
    qtbot.waitExposed(panel)
    assert panel.case_check.isVisible() and panel.scope_buttons["values"].isVisible()


class _LayoutRequests(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.Type.LayoutRequest:
            self.count += 1
        return False


def test_status_label_similar_text_only_repaints(qtbot) -> None:
    """T2: a count / memory text of a similar length does not ask the window for a new
    layout (the label's Qt size hint stays); a much longer one does; the shown text is
    elided, never widening the window."""
    label = StatusLabel()
    qtbot.addWidget(label)
    assert "sizeHint" not in StatusLabel.__dict__ and "minimumSizeHint" not in StatusLabel.__dict__
    label.setText("1 234 567 yozuv")
    hint = label.sizeHint()
    sizing = QLabel.text(label)
    for text in ("1 234 568 yozuv", "1 234 999 yozuv", "1 240 000 yozuv", "1 234 567 yozu"):
        label.setText(text)
        assert label.full_text() == text
        assert QLabel.text(label) == sizing and label.sizeHint() == hint      # no relayout
    label.setText("1 234 567 yozuv (indekslanmoqda 45 %) — juda uzun matn")
    assert label.sizeHint().width() > hint.width()                         # grew: a new layout
    assert label.minimumWidth() < 40                     # the window can always be made narrower
    # a narrow label elides and tells the full text in the tooltip
    label.setFixedWidth(60)
    assert label.shown_text().endswith("…") and label.full_text() in label.toolTip()


def test_status_bar_coalesces_task_updates(qtbot, app, monkeypatch) -> None:
    """T2: task progress (task_updated for every task on every poll) refreshes the status
    bar at most every TASKS_REFRESH_MS, and an unchanged text posts no layout request."""
    bar = StatusBar(app)
    qtbot.addWidget(bar)
    bar.show()
    calls = []
    real = bar.refresh_tasks
    monkeypatch.setattr(bar, "refresh_tasks", lambda: (calls.append(1), real()))
    gate = threading.Event()
    task = app.tasks.run(lambda ctx: gate.wait(20), title="x", kind="test")
    calls.clear()
    for _ in range(50):
        app.tasks.task_updated.emit(task)
    assert calls == []                                           # coalesced: nothing yet
    qtbot.waitUntil(lambda: len(calls) >= 1, timeout=TIMEOUT)
    qtbot.wait(bar.TASKS_REFRESH_MS + 50)
    assert len(calls) <= 3
    bar.count_label.setVisible(True)
    bar.count_label.setText("1 234 yozuv")
    bar.memory_label.setVisible(True)
    bar.memory_label.setText("RAM 101.2 MB")
    QCoreApplication.processEvents()
    counter = _LayoutRequests()
    bar.installEventFilter(counter)
    for i in range(20):
        bar.count_label.setText(f"1 2{i % 10}4 yozuv")
        bar.memory_label.setText(f"RAM 10{i % 10}.3 MB")
        QCoreApplication.processEvents()
    assert counter.count == 0
    gate.set()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)


# ----------------------------------------------------------------------------------------
# T4 / P-F4: closed tabs are freed
# ----------------------------------------------------------------------------------------
def test_closed_tabs_free_their_session_tab_and_views(qtbot, app, tmp_path) -> None:
    from jsonreshape.gui.doctab import default_view_registry

    path = people(tmp_path / "p.json", 2000)
    panels = dict(stub_panels(), search=SearchPanel)          # the real search panel (it keeps results)
    w = MainWindow(app, view_registry=default_view_registry(), panel_factories=panels)
    qtbot.addWidget(w)
    refs = []
    for _ in range(3):
        tab = w.open_path(path, {"ref": NodeRef(3, ())})
        wait_ready(qtbot, tab.session)
        for kind in ("table", "text", "raw", "schema", "tree"):
            tab.set_active_view_kind(kind)
            qtbot.wait(30)
        tab.session.set_filter(FilterSpec("grp == 1"))
        qtbot.waitUntil(lambda s=tab.session: not s.view_busy, timeout=TIMEOUT)
        panel = w.show_search("foo")
        with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT):
            panel.run_search()
        refs.append((weakref.ref(tab), weakref.ref(tab.session), [weakref.ref(v) for v in tab.views()]))
        del tab
    while w.tabs.count():
        w.close_tab(0)
    assert app.tasks.wait_idle(30)
    for _ in range(5):
        flush_deletes()
        gc.collect()
    alive = [(t() is not None, s() is not None, sum(v() is not None for v in vs)) for t, s, vs in refs]
    assert alive == [(False, False, 0)] * 3, alive
    w.close()


# ----------------------------------------------------------------------------------------
# H1 / T3 / T5: exports outlive their dialog, a reopen and a tab close
# ----------------------------------------------------------------------------------------
def test_export_dialog_closed_while_exporting_reports_through_the_runner(qtbot, app, opened, tmp_path,
                                                                        monkeypatch) -> None:
    gate = threading.Event()
    slow_export(monkeypatch, gate)
    s = opened(people(tmp_path / "p.json"))
    parent = QLabel("main window stand-in")
    qtbot.addWidget(parent)
    parent.show()
    dlg = ExportDialog(s, parent, scope="all")
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.show()
    dlg.set_output_path(tmp_path / "out.csv")
    task = dlg.start_export()
    job = dlg.job
    assert task is not None and task.owner is job and job in export_runner(app).jobs(s)
    dlg.reject()
    flush_deletes()
    with pytest.raises(RuntimeError):
        dlg.isVisible()                                  # the dialog is gone
    finished = []
    export_runner(app).job_finished.connect(finished.append)
    gate.set()
    qtbot.waitUntil(lambda: bool(finished), timeout=TIMEOUT)
    assert job.state == "done" and app.errors == []
    assert (tmp_path / "out.csv").read_text(encoding="utf-8-sig").count("\n") == 301
    assert job.report_dialog is not None and job.report_dialog.isVisible()
    assert job.report_dialog.parentWidget() is parent     # next to the window, not the deleted dialog


def test_export_survives_a_reopen_and_the_old_document_closes_after_it(qtbot, app, opened, tmp_path,
                                                                       monkeypatch) -> None:
    gate = threading.Event()
    slow_export(monkeypatch, gate)
    s = opened(people(tmp_path / "p.json"))
    s.set_filter(FilterSpec("grp == 1"))
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    dlg = ExportDialog(s, scope="view")
    qtbot.addWidget(dlg)
    dlg.set_output_path(tmp_path / "view.jsonl")
    task = dlg.start_export()
    assert task is not None
    old_doc = s.doc
    s.reopen()                                            # F5 while the export runs
    wait_ready(qtbot, s)
    assert task.state == "running" and not old_doc.closed   # not detached, document kept
    with qtbot.waitSignal(dlg.export_finished, timeout=TIMEOUT):
        gate.set()
    lines = (tmp_path / "view.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["id"] for x in lines] == [i for i in range(300) if i % 3 == 1]
    qtbot.waitUntil(lambda: old_doc.closed, timeout=TIMEOUT)


def test_node_export_goes_on_after_a_reopen(qtbot, app, opened, tmp_path, monkeypatch) -> None:
    # the node export moved to the writer (stage 5: exportdialog.export_node -> writer.export_node)
    from jsonreshape import writer as writer_mod

    gate = threading.Event()
    real = writer_mod._stream_value

    def slow(*a, **k):
        assert gate.wait(20)
        return real(*a, **k)

    monkeypatch.setattr(writer_mod, "_stream_value", slow)
    s = opened(people(tmp_path / "p.json", 20))
    s.select(NodeRef(4, ()))
    dlg = ExportDialog(s, scope="node")
    qtbot.addWidget(dlg)
    dlg.set_output_path(tmp_path / "node.json")
    task = dlg.start_export()
    assert task is not None
    old_doc = s.doc
    s.reopen(format="array")                     # another format: the session gets a new document
    wait_ready(qtbot, s)
    assert not old_doc.closed
    with qtbot.waitSignal(dlg.export_finished, timeout=TIMEOUT):
        gate.set()
    assert json.loads((tmp_path / "node.json").read_text(encoding="utf-8"))["id"] == 4
    qtbot.waitUntil(lambda: old_doc.closed, timeout=TIMEOUT)


def test_closing_the_tab_lets_the_export_finish(qtbot, app, window, tmp_path, monkeypatch) -> None:
    gate = threading.Event()
    slow_export(monkeypatch, gate)
    path = people(tmp_path / "p.json")
    tab = window.open_path(path)
    wait_ready(qtbot, tab.session)
    dlg = ExportDialog(tab.session, window, scope="all")
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.set_output_path(tmp_path / "all.json")
    task = dlg.start_export()
    doc = tab.session.doc
    window.close_tab(0)
    assert "fonda davom" in window.status.currentMessage()
    assert task.state == "running" and not doc.closed
    gate.set()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)
    assert len(json.loads((tmp_path / "all.json").read_text(encoding="utf-8"))) == 300
    qtbot.waitUntil(lambda: doc.closed, timeout=TIMEOUT)


def test_closing_the_window_with_a_running_export_asks(qtbot, app, window, tmp_path, monkeypatch) -> None:
    gate = threading.Event()
    slow_export(monkeypatch, gate)
    tab = window.open_path(people(tmp_path / "p.json"))
    wait_ready(qtbot, tab.session)
    dlg = ExportDialog(tab.session, window, scope="all")
    qtbot.addWidget(dlg)
    dlg.set_output_path(tmp_path / "all.json")
    task = dlg.start_export()
    asked = []
    window._ask_yes_no = lambda title, text: asked.append(text) or False
    app.headless = False
    try:
        window.close()
        assert asked and not window._closing and task.state == "running"     # "no": stays open
        window._ask_yes_no = lambda title, text: True
        window.close()
        assert window._closing
    finally:
        app.headless = True
        gate.set()
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)


def test_broken_lines_dialog_deleted_while_loading_gets_no_callback(qtbot, app, opened, tmp_path,
                                                                   monkeypatch) -> None:
    """H1 (same pattern): a dialog destroyed without done() (e.g. with its tab) detaches
    its task, so the late result never touches the deleted widgets."""
    lines = []
    for i in range(400):
        lines.append('{"id": %d, "bad": }' % i if i % 2 else json.dumps({"id": i}))
    path = tmp_path / "broken.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    s = opened(path)
    gate = threading.Event()
    real = type(s.doc).iter_broken_lines

    def slow(self):
        gate.wait(20)
        yield from real(self)

    monkeypatch.setattr(type(s.doc), "iter_broken_lines", slow)
    assert s.doc.broken_count == 200
    s.doc.broken_lines = s.doc.broken_lines[:3]           # as if more than the in-memory part existed
    holder = QLabel()
    dlg = BrokenLinesDialog(s, holder)
    assert dlg._task is not None
    task = dlg._task
    holder.deleteLater()                                  # the parent (tab) goes away
    flush_deletes()
    gate.set()
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    qtbot.wait(50)
    assert app.errors == []


# ----------------------------------------------------------------------------------------
# H2: no search / export of a view that is still being computed
# ----------------------------------------------------------------------------------------
def test_search_and_view_export_wait_for_a_running_filter(qtbot, app, opened, tmp_path, monkeypatch) -> None:
    import jsonreshape.gui.session as session_mod

    gate = threading.Event()
    real = session_mod.filter_records

    def slow(doc, flt, src=None, **kw):
        gate.wait(20)
        return real(doc, flt, src, **kw)

    monkeypatch.setattr(session_mod, "filter_records", slow)
    s = opened(people(tmp_path / "p.json"))
    panel = SearchPanel(app)
    qtbot.addWidget(panel)
    panel.set_session(s)
    s.set_filter(FilterSpec("grp == 2"))
    assert s.view_busy
    panel.set_options({"text": "foo"})
    assert panel.run_search() is None
    assert panel.error_text() == t("gui.search.view_busy")
    dlg = ExportDialog(s, scope="view")
    qtbot.addWidget(dlg)
    dlg.set_output_path(tmp_path / "v.json")
    assert not dlg.export_button.isEnabled() and t("gui.export.view_busy") in dlg.error_text()
    assert dlg.start_export() is None
    gate.set()
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert dlg.export_button.isEnabled()
    with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT):
        assert panel.run_search() is not None
    assert all(h.record % 3 == 2 for h in panel.state().hits) and panel.state().hits


# ----------------------------------------------------------------------------------------
# M1: aliases of the source are refused
# ----------------------------------------------------------------------------------------
def test_node_export_refuses_an_alias_of_the_source(qtbot, app, opened, tmp_path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    src = real_dir / "src16.json"
    src.write_bytes('{"a": {"b": [1, 2, 3]}, "keep": "ORIGINAL"}'.encode("utf-16"))
    before = src.read_bytes()
    s = opened(src)
    aliases = []
    hard = tmp_path / "hard.json"
    os.link(src, hard)
    aliases.append(hard)
    if os.name == "nt":
        junction = tmp_path / "jn"
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(real_dir)], capture_output=True)
        if made.returncode == 0:
            aliases.append(junction / "src16.json")
    for alias in aliases:
        with pytest.raises(UserInputError) as info:
            export_node(s, NodeRef(None, ("a",)), alias, "json")
        assert info.value.key == "writer.error.same_file"
        dlg = ExportDialog(s, scope="all")
        qtbot.addWidget(dlg)
        dlg.set_output_path(alias)
        assert dlg.start_export() is None and "o'zgarmaydi" in dlg.error_text()
    assert src.read_bytes() == before


# ----------------------------------------------------------------------------------------
# M3 / L6 / C10 / C11: search panel
# ----------------------------------------------------------------------------------------
def test_cancelled_search_without_hits_is_stale_after_reopen(qtbot, app, opened, tmp_path, monkeypatch) -> None:
    s = opened(people(tmp_path / "p.json"))
    panel = SearchPanel(app)
    qtbot.addWidget(panel)
    panel.set_session(s)
    real = search_mod.search

    def stuck(*a, **k):
        cancel = k["cancel"]
        while not cancel.cancelled:
            time.sleep(0.005)
        cancel.check()

    monkeypatch.setattr(search_mod, "search", stuck)
    panel.set_options({"text": "zzz"})
    with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT):
        panel.run_search()
        qtbot.wait(30)
        panel.cancel_search()
    monkeypatch.setattr(search_mod, "search", real)
    assert panel.state().status == "cancelled" and panel.export_button.isEnabled()
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen()
    st = panel.state()
    assert st.status == "stale" and st.opts is None
    assert not panel.export_button.isEnabled()
    assert panel.export_hits_to(str(tmp_path / "x.csv")) is None and not (tmp_path / "x.csv").exists()


def test_search_panel_source_label_inputs_and_modes(qtbot, app, opened, tmp_path) -> None:
    a = opened(people(tmp_path / "a.json"))
    b = opened(people(tmp_path / "b.json", 50))
    panel = SearchPanel(app)
    qtbot.addWidget(panel)
    panel.set_session(a)
    a.set_filter(FilterSpec("grp == 0"))
    qtbot.waitUntil(lambda: not a.view_busy, timeout=TIMEOUT)
    panel.set_options({"text": "foo", "scope": "values"})
    with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT):
        panel.run_search()
    searched = panel.source_label.full_text()
    assert "joriy ko'rinish" in searched and searched.count("(100 ta yozuv)") == 1      # C10: once
    a.reset_view()
    text = panel.source_label.full_text()
    assert text.startswith("Qidirilgan:") and "Filtr" in text and "barcha yozuvlar" in text   # L6
    # inputs per tab: b gets its own, a's come back with a's results
    panel.set_session(b)
    panel.set_options({"text": "bar7", "scope": "both"})
    panel.set_session(a)
    assert panel.query_text() == "foo" and panel.scope() == "values" and panel.hit_count_shown() > 0
    panel.set_session(b)
    assert panel.query_text() == "bar7" and panel.hit_count_shown() == 0
    # number mode: "keys" is not available (and comes back afterwards)
    panel.set_options({"scope": "keys"})
    panel.set_options({"mode": "number", "text": "> 5"})
    assert not panel.scope_buttons["keys"].isEnabled() and panel.scope() == "values"
    panel.set_options({"mode": "plain", "text": "x"})
    assert panel.scope_buttons["keys"].isEnabled() and panel.scope() == "keys"
    # regex mode: a checked (disabled) translit box is not part of the search
    panel.set_options({"mode": "plain", "translit": True})
    panel.set_options({"mode": "regex", "text": "fo+"})
    assert panel.translit_check.isChecked() and not panel.options().query.translit
    # an error replaces the previous summary
    panel.set_session(a)
    assert panel.summary_label.isVisibleTo(panel)
    panel.set_options({"mode": "regex", "text": "(abc"})
    assert panel.error_text() and not panel.summary_label.isVisibleTo(panel)
    panel.set_options({"text": "abc"})
    assert not panel.error_text() and panel.summary_label.isVisibleTo(panel)


def test_open_as_records_disabled_for_single_documents_and_export_after_cancel_asks(qtbot, app, opened, tmp_path,
                                                                                    monkeypatch) -> None:
    single = opened(write_json(tmp_path / "one.json", "just foo"))
    assert single.doc.kind == "single"
    panel = SearchPanel(app)
    qtbot.addWidget(panel)
    panel.set_session(single)
    panel.set_options({"text": "foo"})
    with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT):
        panel.run_search()
    assert panel.state().hits and not panel.open_button.isEnabled()
    assert panel.open_button.toolTip() == t("gui.search.open_records.single")
    # export after a cancelled search: the user is told it searches again
    st = panel.state()
    st.status = "cancelled"
    panel._refresh()
    assert panel.export_button.toolTip() == t("gui.search.export.rerun_tip")
    asked = []
    panel._confirm = lambda text: asked.append(text) or False
    from PySide6.QtWidgets import QFileDialog

    out = str(tmp_path / "hits.csv")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (out, "")))
    panel.choose_export_file()
    assert asked == [t("gui.search.export.rerun_confirm")] and not os.path.exists(out)
    panel._confirm = lambda text: True
    with qtbot.waitSignal(panel.export_finished, timeout=TIMEOUT):
        panel.choose_export_file()
    assert os.path.exists(out)


def test_search_export_count_is_a_python_int_and_survives_a_reopen(qtbot, app, opened, tmp_path) -> None:
    """T11 (object signal) + T5 for the search export (not owned by the session)."""
    s = opened(people(tmp_path / "p.json"))
    panel = SearchPanel(app)
    qtbot.addWidget(panel)
    panel.set_session(s)
    panel.set_options({"text": "foo"})
    with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT):
        panel.run_search()
    got = []
    panel.export_finished.connect(lambda path, count: got.append(count))
    panel.export_finished.emit("x", 2 ** 40)
    assert got == [2 ** 40]
    got.clear()
    task = panel.export_hits_to(str(tmp_path / "hits.json"))
    assert task.owner is panel
    s.reopen()
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    assert got == [30] and len(json.loads((tmp_path / "hits.json").read_text(encoding="utf-8"))) == 30
    wait_ready(qtbot, s)


# ----------------------------------------------------------------------------------------
# M5 / A-F4 / K2: session view pipeline
# ----------------------------------------------------------------------------------------
def test_filter_and_sort_work_on_the_shown_search_results(qtbot, app, opened, tmp_path) -> None:
    s = opened(people(tmp_path / "p.json", 200))
    panel = SearchPanel(app)
    qtbot.addWidget(panel)
    panel.set_session(s)
    panel.set_options({"text": "foo"})
    with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT):
        panel.run_search()
    with qtbot.waitSignal(panel.records_opened, timeout=TIMEOUT):
        panel.open_as_records()
    hits = [s.record_at(i) for i in range(s.view_len())]
    assert hits == list(range(0, 200, 10))
    s.set_sort([SortKey("id", descending=True)])
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert [s.record_at(i) for i in range(s.view_len())] == hits[::-1]
    s.set_filter(FilterSpec("id < 100"))
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert [s.record_at(i) for i in range(s.view_len())] == [90, 80, 70, 60, 50, 40, 30, 20, 10, 0]
    label = s.view_label()
    assert label.startswith("Qidiruv natijasi: “foo”") and "Filtr: id < 100" in label
    s.set_filter(None)
    s.set_sort(None)
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert [s.record_at(i) for i in range(s.view_len())] == hits     # still the search results
    s.reset_view()
    assert s.view_is_base and s.view_len() == 200


def test_reopen_with_another_record_path_clears_quick_filters(qtbot, app, opened, tmp_path) -> None:
    s = opened(write_json(tmp_path / "two.json", {"data": records(40), "other": records(6)}))
    s.set_quick_filters({"id": "id < 20", "name": 'name == "n1"'})
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert s.view_len() == 3 and s.quick_filters == {"id": "id < 20", "name": 'name == "n1"'}
    s.reopen(record_path="other")
    wait_ready(qtbot, s)
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert s.quick_filters == {} and s.filter is None and s.view_is_base and s.view_len() == 6
    # F5 with the same options keeps them
    s.set_filter(FilterSpec("id > 2"))
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    s.reopen()
    wait_ready(qtbot, s)
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 3, timeout=TIMEOUT)
    assert s.filter is not None and s.filter.text == "id > 2"


def test_set_quick_filters_runs_one_pipeline(qtbot, app, opened, tmp_path) -> None:
    s = opened(people(tmp_path / "p.json"))
    started = []
    app.tasks.task_added.connect(lambda task: started.append(task.kind))
    s.set_quick_filters({"grp": "grp == 1", "id": "id < 100", "empty": "  "})
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert started.count("filter") == 1
    assert [s.record_at(i) for i in range(s.view_len())] == [i for i in range(100) if i % 3 == 1]
    assert s.quick_filters == {"grp": "grp == 1", "id": "id < 100"}
    errors = len(app.errors)
    s.set_quick_filters({"grp": "grp =="})                        # invalid: reported, nothing changes
    assert len(app.errors) == errors + 1 and s.quick_filters == {"grp": "grp == 1", "id": "id < 100"}
    s.set_quick_filters({})
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert s.view_is_base


@pytest.mark.parametrize("n_records", [5, 200])
def test_positions_of_matches_find_position(qtbot, app, opened, tmp_path, n_records) -> None:
    rs = RecordSet.from_indices([7, 3, 9, 1, 250, 42, 8, 0] + list(range(500, 900)))
    wanted = [7, 0, 250, 899, 11, 1234, -1] + list(range(500, 500 + n_records))
    got = find_positions(rs, wanted)
    assert got == {r: find_position(rs, r) for r in wanted}
    s = opened(people(tmp_path / "p.json"))
    s.set_sort([SortKey("id", descending=True)])
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    result = []
    s.positions_of([0, 299, 150, 400], result.append)
    qtbot.waitUntil(lambda: bool(result), timeout=TIMEOUT)
    assert result == [{0: 299, 299: 0, 150: 149, 400: None}]


def test_cancelled_filter_is_told_in_the_status_bar(qtbot, app, opened, tmp_path, monkeypatch) -> None:
    import jsonreshape.gui.session as session_mod

    real = session_mod.filter_records

    def slow(doc, flt, src=None, *, cancel=None, **kw):
        while not cancel.cancelled:
            time.sleep(0.005)
        cancel.check()

    s = opened(people(tmp_path / "p.json"))
    messages = []
    app.status_message.connect(lambda text, ms: messages.append(text))
    monkeypatch.setattr(session_mod, "filter_records", slow)
    s.set_filter(FilterSpec("grp == 1"))
    task = next(x for x in app.tasks.tasks(owner=s) if x.kind == "filter")
    task.cancel()                                                  # e.g. from the tasks panel
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    monkeypatch.setattr(session_mod, "filter_records", real)
    assert s.view_is_base and s.filter is None
    assert messages and "bekor qilindi" in messages[-1]


def test_record_not_found_says_so_far_only_while_indexing(qtbot, app, opened, tmp_path) -> None:
    s = opened(people(tmp_path / "p.json", 10))
    with pytest.raises(UserInputError) as info:
        s.node(NodeRef(50, ()))
    assert info.value.key == "gui.session.record_not_found" and "hozircha" not in info.value.message


# ----------------------------------------------------------------------------------------
# T9: GUI state saved off the GUI thread
# ----------------------------------------------------------------------------------------
def test_gui_state_save_does_not_block_get_set_and_keeps_new_changes(tmp_path) -> None:
    from jsonreshape.query import file_lock

    path = tmp_path / "gui_state.json"
    state = GuiState.load(path)
    state.set("a", 1)
    holder_ready = threading.Event()
    release = threading.Event()

    def other_window() -> None:
        with file_lock(path, "gui.state.busy", timeout=5):
            holder_ready.set()
            release.wait(5)

    other = threading.Thread(target=other_window)
    other.start()
    assert holder_ready.wait(5)
    saver = threading.Thread(target=state.save)
    saver.start()
    time.sleep(0.1)                                            # the save waits for the file lock
    started = time.perf_counter()
    state.set("b", 2)                                          # the GUI thread is not blocked
    assert state.get("a") == 1
    assert time.perf_counter() - started < 0.2
    release.set()
    saver.join(10)
    other.join(10)
    on_disk = GuiState.load(path)
    assert on_disk.get("a") == 1 and on_disk.get("b") is None   # "b" came after the save started
    assert state.dirty and state.get("b") == 2
    assert state.save() and GuiState.load(path).get("b") == 2 and not state.dirty


def test_save_state_is_a_coalesced_silent_task(qtbot, app, monkeypatch) -> None:
    calls = []
    gate = threading.Event()
    real = app.state.save

    def slow_save(*a, **k):
        calls.append(threading.current_thread() is threading.main_thread())
        gate.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(app.state, "save", slow_save)
    first = app.save_state()
    assert first.silent and first.kind == "state"
    for _ in range(10):
        assert app.save_state() is first
    gate.set()
    qtbot.waitUntil(lambda: not app.state_saving, timeout=TIMEOUT)
    assert calls == [False, False]                             # one more save for the 10 requests


# ----------------------------------------------------------------------------------------
# L4 / C9 / C11: document tab bars and banner
# ----------------------------------------------------------------------------------------
def test_no_duplicate_bars_and_a_closed_grown_bar_stays_closed(qtbot, app, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(DocSession, "CHANGE_POLL_MS", 80)
    wrapped = wrapped_file(tmp_path / "w.json", 20)
    s = DocSession(app, wrapped, use_cache=False)
    tab = DocumentTab(s, view_registry=stub_registry())
    qtbot.addWidget(tab)
    s.start()
    wait_ready(qtbot, s)
    auto_texts = [text for key, text in s.warning_items() if key == "reader.record_path_auto"]
    assert auto_texts and tab.info_bar("record_path") is not None
    bar = tab.info_bar("warnings")
    shown = "" if bar is None else bar.message_label.text() + " | " + bar.detail_label.text()
    assert all(text not in shown for text in auto_texts)        # C9: said once (record path bar)
    tab.shutdown()
    log = tmp_path / "log.jsonl"
    log.write_text('{"a": 1}\n', encoding="utf-8")
    s2 = DocSession(app, log, use_cache=False)
    tab2 = DocumentTab(s2, view_registry=stub_registry())
    qtbot.addWidget(tab2)
    s2.start()
    wait_ready(qtbot, s2)
    with open(log, "ab") as f:
        f.write(b'{"a": 2}\n')
    qtbot.waitUntil(lambda: tab2.info_bar("file_changed") is not None, timeout=TIMEOUT)
    qtbot.wait(100)
    assert tab2.info_bar("warnings") is None                   # L4: the grown bar says it
    tab2.info_bar("file_changed").close_bar()
    with open(log, "ab") as f:
        f.write(b'{"a": 3}\n')
    qtbot.waitUntil(lambda: s2.doc.appended_bytes > 9 and s2._last_appended > 9, timeout=TIMEOUT)
    qtbot.wait(300)
    assert tab2.info_bar_keys() == []                          # closed: does not come back
    with qtbot.waitSignal(s2.index_finished, timeout=TIMEOUT):
        tab2.reopen()
    tab2.shutdown()


def test_view_banner_tells_a_filtered_view_and_clears_it(qtbot, app, tmp_path) -> None:
    s = DocSession(app, people(tmp_path / "p.json"), use_cache=False)
    tab = DocumentTab(s, view_registry=stub_registry())
    qtbot.addWidget(tab)
    s.start()
    wait_ready(qtbot, s)
    assert tab.view_banner.isHidden()
    s.set_filter(FilterSpec("grp == 1"))
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not tab.view_banner.isHidden(), timeout=TIMEOUT)
    assert tab.view_banner.label.full_text() == "Ko'rinish: Filtr: grp == 1 · 100 / 300 yozuv"
    tab.view_banner.clear_button.click()
    assert s.view_is_base and tab.view_banner.isHidden()
    tab.shutdown()


# ----------------------------------------------------------------------------------------
# L9 / M7 / C7 / L10 / C12: export path, fonds, numbers, command line, wording
# ----------------------------------------------------------------------------------------
def test_relative_export_path_uses_the_source_folder(qtbot, app, opened, tmp_path, monkeypatch) -> None:
    folder = tmp_path / "src"
    folder.mkdir()
    s = opened(people(folder / "p.json", 5))
    monkeypatch.chdir(tmp_path)
    dlg = ExportDialog(s, scope="all")
    qtbot.addWidget(dlg)
    dlg.set_output_path("out.csv")
    assert dlg.output_path() == str(folder / "out.csv")
    app.state.set("last_export_dir", str(tmp_path))
    assert dlg.output_path() == str(tmp_path / "out.csv")


def test_settings_change_reapplies_the_ui_font(qtbot, app, window) -> None:
    before = QApplication.font()
    try:
        app.state.set("ui_size", 17)
        app.settings_changed.emit()
        assert QApplication.font().pointSizeF() == 17.0
        assert window.status.count_label.minimumHeight() <= window.status.count_label.sizeHint().height()
    finally:
        app.state.set("ui_size", None)
        QApplication.setFont(before)


def test_number_texts(lang_uz) -> None:
    assert format_bytes(999_950) == "1.0 MB" and format_bytes(999_999_999) == "1.0 GB"
    assert format_bytes(1500) == "1.5 KB" and format_bytes(float("inf")) == "? B"
    assert format_duration(59.6) == "1:00" and format_duration(float("inf")) == "?"
    assert eta_text(3 * 86400 + 4 * 3600) == "3 kun 4 soat" and eta_text(float("nan")) == ""
    info = ProgressInfo(done=2, total=100, fraction=0.02, rate=0.4, eta=245.0, elapsed=5.0, message="",
                        unit="records", finished=False)
    assert progress_texts(info)[1] == "0.4/s"
    assert bar_value(0.9996) == 999 and bar_value(1.0, finished=True) == 1000
    assert title_text("Indekslanmoqda: a.json", "Indekslanmoqda: a.json") == "Indekslanmoqda: a.json"
    assert title_text("Eksport: a.csv", "ustunlar") == "Eksport: a.csv — ustunlar"
    assert "o'rama" in t("gui.export.keep_wrapper")                   # C12


def test_status_bar_tells_a_cancelled_index(qtbot, app, tmp_path) -> None:
    s = DocSession(app, people(tmp_path / "p.json"), use_cache=False)
    bar = StatusBar(app)
    qtbot.addWidget(bar)
    tab = DocumentTab(s, view_registry=stub_registry())
    qtbot.addWidget(tab)
    bar.set_tab(tab)
    s.start()
    wait_ready(qtbot, s)
    from jsonreshape.errors import Cancelled

    s._failed(Cancelled())                               # the index task was cancelled
    assert "to'xtatildi" in bar.count_label.full_text() and "300 yozuv" in bar.count_label.full_text()
    tab.shutdown()


def test_command_line_and_dropped_folders(qtbot, app, window, tmp_path) -> None:
    a = write_json(tmp_path / "a.json", records(3))
    tabs = window.open_command_line(["", str(a), str(a).upper() if os.name == "nt" else str(a), str(tmp_path)])
    assert len(tabs) == 1 and window.tabs.count() == 1
    assert "Papka" in window.status.currentMessage()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path))])
    drop = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton,
                      Qt.KeyboardModifier.NoModifier)
    window.status.clearMessage()
    window.dropEvent(drop)
    assert window.tabs.count() == 1 and "Papka" in window.status.currentMessage()
    wait_ready(qtbot, tabs[0].session)


def test_closed_tab_releases_its_dialogs(qtbot, app, window, tmp_path) -> None:
    tab = window.open_path(write_json(tmp_path / "a.json", records(3)))
    wait_ready(qtbot, tab.session)
    window.show_goto("#1")
    assert window.dialogs["goto"].session is tab.session
    window.close_tab(0)
    assert "goto" not in window.dialogs
