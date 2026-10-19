"""CompareWindow / CompareFilesDialog / the compare mark (jsonreshape/gui/comparewindow.py;
ARCHITECTURE §14.9 "Compare", s5/map_repair_diff.md §4.7).

Mark & compare two nodes (rows coloured by role, F7 / Shift+F7), the node-menu items, "compare
the two selected"; the files dialog (sources, key list from the schema with ``id`` preselected,
validation, browse seam); a file comparison (heavy task) -> results grid (status / key / A# / B# /
changes / first path, read window by window) -> the selected pair in the DiffView, filters,
F7 across records, "open as a record set" in both tabs, export (JSON / JSON Lines / CSV, the
source refused), cancel, an edited tab compared as shown, stale results (an edit, a closed
tab) and refresh, single documents compared as nodes, the wrapper row, the "too big without a
key" error with its hint, swap and options, the results file removed when the window closes;
both core backends.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jsonreshape import codec, reader  # noqa: E402
from jsonreshape.diff import DiffOptions  # noqa: E402
from jsonreshape.gui import comparewindow as cw  # noqa: E402
from jsonreshape.gui import widgets  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.session import DocSession, NodeRef  # noqa: E402
from jsonreshape.settings import Settings  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000


# ----------------------------------------------------------------------------------------
# data
# ----------------------------------------------------------------------------------------
def record_text(i: int, name: str | None = None) -> str:
    return ('{"id": %d, "name": "%s", "city": "%s", "nums": [1.10, 1e400, 12345678901234567890, -0.0]}'
            % (i, name if name is not None else f"n{i}", ["Toshkent", "Oʻzbekiston", "Самарқанд"][i % 3]))


def write_records(path: Path, texts: list[str]) -> Path:
    path.write_text("[\n" + ",\n".join(texts) + "\n]", encoding="utf-8")
    return path


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    a = [record_text(i) for i in range(40)]
    b = list(a)
    b[5] = record_text(5, "o'zgardi")
    b[20] = b[20].replace("1.10", "1.1")          # equal by value
    del b[10]
    b.append('{"id": 99, "name": "yangi", "city": "Xiva", "nums": []}')
    return write_records(tmp_path / "a.json", a), write_records(tmp_path / "b.json", b)


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.statuses = []
    ctx.error_reported.connect(ctx.errors.append)
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    yield ctx
    for w in list(widgets._kept_alive):
        w.close()
    assert ctx.tasks.shutdown(30)
    assert not ctx.errors, ctx.errors


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
def windows(qtbot):
    made: list[cw.CompareWindow] = []

    def track(win: cw.CompareWindow | None) -> cw.CompareWindow:
        assert win is not None
        made.append(win)
        return win

    yield track
    for w in made:
        if cw._alive(w):
            w.close()


@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    yield name
    reader.set_scanner_backend(old)


def new_window(app: Any, windows: Any) -> cw.CompareWindow:
    win = windows(cw.CompareWindow(app))
    win.resize(1100, 800)
    win.show()
    return win


def run_files(qtbot, win: cw.CompareWindow, spec: cw.CompareSpec) -> Any:
    with qtbot.waitSignal(win.run_finished, timeout=TIMEOUT) as blocker:
        assert win.compare_files(spec) is not None
    return blocker.args[0]


def wait_view(qtbot, win: cw.CompareWindow) -> None:
    qtbot.waitUntil(lambda: not win.view.is_loading() and (win.view.model() is not None
                                                           or win.view.error() is not None), timeout=TIMEOUT)


def wait_rows(qtbot, win: cw.CompareWindow) -> list[Any]:
    """Every result row loaded (rows outside the visible window are asked for too)."""
    def ready() -> bool:
        entries = [e for e in (win.entry_of(r) for r in range(win._result_rows())) if isinstance(e, int)]
        missing = [e for e in entries if not isinstance(win._loader.peek(e), cw._ResultRow)]
        if missing:
            win._loader.request(missing)
        return not missing

    qtbot.waitUntil(ready, timeout=TIMEOUT)
    return [win.result_row(r) for r in range(win._result_rows())]


def select_row(qtbot, win: cw.CompareWindow, row: int) -> None:
    win.results.set_current(row, 0)
    win._show_detail()
    wait_view(qtbot, win)


def view_rows(win: cw.CompareWindow) -> dict[str, Any]:
    return {r.key_label: r for r in win.view.model().iter_rows()}


# ----------------------------------------------------------------------------------------
# marks and node comparisons
# ----------------------------------------------------------------------------------------
def test_mark_and_compare_two_nodes(qtbot, app, opened, files, windows) -> None:
    sa = opened(files[0])
    assert cw.marked(app) is None
    mark = cw.mark_for_compare(app, sa, NodeRef(3, ()))
    assert cw.marked(app) is mark and mark.label == "a.json · #3"
    assert any("Taqqoslash uchun belgilandi: a.json · #3" in s for s in app.statuses)
    win = windows(cw.compare_with_marked(None, sa, NodeRef(5, ()), app=app))
    wait_view(qtbot, win)
    assert win.windowTitle() == "Taqqoslash: a.json · #3 ↔ a.json · #5"
    rows = view_rows(win)
    assert (rows["id"].a_text, rows["id"].b_text, rows["id"].role) == ("3", "5", "diff_changed")
    assert rows["nums"].status == "equal" and rows["nums"].summary
    assert rows["city"].status == "changed" and rows["city"].role == "diff_changed"
    # F7 / Shift+F7 are window shortcuts
    win.activateWindow()
    win.view.grid.setFocus()
    qtbot.waitUntil(win.view.grid.hasFocus, timeout=3000)
    first = win.view.current_row().key_label
    QTest.keyClick(win.view.grid, Qt.Key.Key_F7)
    second = win.view.current_row().key_label
    assert (first, second) == ("id", "name")
    QTest.keyClick(win.view.grid, Qt.Key.Key_F7, Qt.KeyboardModifier.ShiftModifier)
    assert win.view.current_row().key_label == "id"
    cw.clear_mark(app)
    assert cw.marked(app) is None
    assert cw.compare_with_marked(None, sa, NodeRef(5, ()), app=app) is None
    assert any("belgilangan tugun yo'q" in s for s in app.statuses)


def test_mark_dies_with_its_tab(qtbot, app, opened, files) -> None:
    sa = opened(files[0])
    cw.mark_for_compare(app, sa, NodeRef(1, ("name",)))
    sa.close()
    assert cw.marked(app) is None


def test_node_menu_items(qtbot, app, opened, files, windows) -> None:
    sa = opened(files[0])
    items = cw.node_menu_items(None, sa, NodeRef(2, ()), app=app)
    assert [i[0] for i in items] == ["Taqqoslash uchun belgilash", "Belgilangan bilan taqqoslash"]
    assert items[1][2] is False and "Avval" in items[1][3]
    items[0][1]()                                              # mark #2
    same = cw.node_menu_items(None, sa, NodeRef(2, ()), app=app)
    assert same[1][2] is False
    other = cw.node_menu_items(None, sa, NodeRef(4, ("name",)), app=app)
    assert other[1][2] is True and other[1][3] == "Belgilangan: a.json · #2"
    win = windows(other[1][1]())
    wait_view(qtbot, win)
    assert win.view.model() is not None


def test_compare_selected_needs_exactly_two(qtbot, app, opened, files, windows) -> None:
    sa = opened(files[0])
    assert cw.compare_selected(None, sa, [NodeRef(1, ())], app=app) is None
    win = windows(cw.compare_selected(None, sa, [NodeRef(1, ("nums",)), NodeRef(2, ("nums",))], app=app))
    wait_view(qtbot, win)
    assert win.view.model().equal and "Farq yo'q" in win.view.message()


def test_node_compare_swap_and_reveal(qtbot, app, opened, files, windows) -> None:
    sa = opened(files[0])
    sb = opened(files[1])
    revealed: list[tuple[Any, NodeRef]] = []
    win = windows(cw.compare_nodes(None, (sa, NodeRef(None, ())), (sb, NodeRef(None, ())), app=app,
                                   reveal=lambda s, ref: revealed.append((s, ref))))
    wait_view(qtbot, win)
    m = win.view.model()
    changed = next(r for r in m.iter_rows() if r.status == "changed")
    assert changed.a_path == (5, "name") and changed.a_text == '"n5"' and changed.b_text == '"o\'zgardi"'
    menu = win.view.build_menu(changed.row)
    next(a for a in menu.actions() if a.objectName() == "diff_reveal_b").trigger()
    assert revealed == [(sb, NodeRef(None, (5, "name")))]
    win.view.swap()
    wait_view(qtbot, win)
    changed = next(r for r in win.view.model().iter_rows() if r.status == "changed")
    assert changed.a_text == '"o\'zgardi"'


# ----------------------------------------------------------------------------------------
# the files dialog
# ----------------------------------------------------------------------------------------
def test_files_dialog(qtbot, app, opened, files, tmp_path) -> None:
    sa = opened(files[0])
    sb = opened(files[1])
    dlg = cw.show_compare_files(None, sa, sessions=[sa, sb], app=app)
    qtbot.addWidget(dlg)
    combo_a, path_a, _ = dlg.side_widgets["a"]
    combo_b, path_b, browse_b = dlg.side_widgets["b"]
    assert combo_a.currentData() is sa and combo_a.currentText() == "a.json (joriy tab)"
    assert combo_b.currentData() is sb and path_b.text() == sb.path and path_b.isReadOnly()
    qtbot.waitUntil(lambda: "id" in dlg.key_fields(), timeout=TIMEOUT)
    assert dlg.key_combo.currentText() == "id"
    assert "name" in dlg.key_fields() and not any("[*]" in f for f in dlg.key_fields())
    spec = dlg.spec()
    assert (spec.a.session, spec.b.session, spec.mode, spec.key) == (sa, sb, "auto", "id")
    # a file side through the browse seam
    other = write_records(tmp_path / "c.json", [record_text(1)])
    dlg.ask_open_path = lambda: str(other)
    browse_b.setEnabled(True)
    combo_b.setCurrentIndex(combo_b.count() - 1)
    dlg._browse("b")
    assert path_b.text() == str(other) and not path_b.isReadOnly()
    assert dlg.spec().b.session is None and Path(dlg.spec().b.path) == other
    # validation
    path_b.setText(str(tmp_path / "missing.json"))
    dlg.accept()
    assert "Fayl topilmadi" in dlg.error_text() and dlg.isVisible()
    path_b.setText(str(other))
    dlg.mode_combo.setCurrentIndex(dlg.mode_combo.findData("key"))
    dlg.key_combo.setEditText("")
    dlg.accept()
    assert "yozuv kalitini kiriting" in dlg.error_text()
    dlg.key_combo.setEditText("a[")
    dlg.accept()
    assert dlg.error_text()
    dlg.key_combo.setEditText("id")
    dlg.array_combo.setCurrentIndex(dlg.array_combo.findData("key"))
    assert dlg.array_key.isEnabled()
    dlg.accept()
    assert "maydonini ko'rsating" in dlg.error_text()
    dlg.array_key.setText("tags=name")
    dlg.numbers_check.setChecked(True)
    spec = dlg.spec()
    assert spec.opts.array_match == "key" and spec.opts.numbers == "text" and spec.mode == "key"
    dlg.accept()
    win = getattr(dlg, "window_opened", None)
    assert isinstance(win, cw.CompareWindow)
    qtbot.waitUntil(lambda: win.run_state() != "running", timeout=TIMEOUT)
    win.close()


# ----------------------------------------------------------------------------------------
# file comparisons
# ----------------------------------------------------------------------------------------
def test_file_compare_results_and_details(qtbot, app, opened, files, windows, backend) -> None:
    sa = opened(files[0])
    win = new_window(app, windows)
    spec = cw.CompareSpec(cw.FileSide(session=sa, path=sa.path), cw.FileSide(path=str(files[1])), mode="key", key="id")
    rep = run_files(qtbot, win, spec)
    assert (rep.changed, rep.added, rep.removed, rep.equal) == (1, 1, 1, 38)
    assert win.windowTitle() == "Taqqoslash: a.json ↔ b.json"
    assert win.summary.full_text().startswith("1 ta o'zgargan, 1 ta qo'shilgan, 1 ta o'chirilgan, 38 ta bir xil")
    assert "kalit bo'yicha: id" in win.summary.full_text()
    assert [b.text() for b in win.filter_checks.values()] == ["O'zgargan (1)", "Qo'shilgan (1)", "O'chirilgan (1)"]
    rows = wait_rows(qtbot, win)
    got = [(r.status, r.key, r.a, r.b, r.changes, r.path) for r in rows]
    assert got == [("changed", "5", 5, 5, 1, "name"), ("removed", "10", 10, None, 1, ""),
                   ("added", "99", None, 39, 1, "")]
    status_cell = win.results.source().cell(0, win.results.columns()[0])
    assert (status_cell.text, status_cell.bg) == ("o'zgargan", "diff_changed")
    # the first row is shown at once
    wait_view(qtbot, win)
    m = win.view.model()
    assert m.root_label == "#5 ↔ #5 · 5"
    name = view_rows(win)["name"]
    assert (name.a_text, name.b_text, name.status) == ('"n5"', '"o\'zgardi"', "changed")
    assert [c.title for c in win.view.grid.columns()] == ["Kalit", "A: a.json #5", "B: b.json #5"]
    assert not win.view.match_combo.isEnabled()                 # the run's options
    select_row(qtbot, win, 1)
    root = win.view.model().row(0)
    assert (root.key_label, root.status, root.point, root.b_text) == ("#10 · 10", "removed", True, "")
    select_row(qtbot, win, 2)
    assert win.view.model().row(0).status == "added"
    # filters
    win.filter_checks["changed"].setChecked(False)
    qtbot.waitUntil(lambda: win._result_rows() == 2, timeout=TIMEOUT)
    assert [win.entry_of(r) for r in range(2)] == [1, 2]
    win.filter_checks["added"].setChecked(False)
    qtbot.waitUntil(lambda: win._result_rows() == 1, timeout=TIMEOUT)
    for box in win.filter_checks.values():
        box.setChecked(True)
    qtbot.waitUntil(lambda: win._result_rows() == 3, timeout=TIMEOUT)
    # F7 at the last difference of a record goes on to the next record
    select_row(qtbot, win, 0)
    win.view.grid.set_current(name.row, 0)
    win.next_change()
    wait_view(qtbot, win)
    assert win.results.current()[0] == 1 and win.view.model().row(0).status == "removed"
    win.prev_change()
    wait_view(qtbot, win)
    qtbot.waitUntil(lambda: win.view.current_row() is not None and win.view.current_row().key_label == "name",
                    timeout=TIMEOUT)
    assert win.results.current()[0] == 0
    # closing the window removes the results file; no thread of the window lives on
    path = rep.results_file
    assert path is not None and path.exists()
    win.close()
    qtbot.waitUntil(lambda: not path.exists(), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not [th for th in threading.enumerate()
                                 if th.name.startswith(("RowLoader-compare", "jsonreshape-compare"))], timeout=TIMEOUT)


def test_position_mode_jsonl_against_array(qtbot, app, files, tmp_path, windows, backend) -> None:
    jsonl = tmp_path / "a.jsonl"
    recs = codec.loads(files[0].read_bytes())                    # exact numbers (1e400, 1.10, …)
    jsonl.write_bytes(b"".join(codec.dumps(r) + b"\n" for r in recs))
    win = new_window(app, windows)
    rep = run_files(qtbot, win, cw.CompareSpec(cw.FileSide(path=str(files[0])), cw.FileSide(path=str(jsonl)),
                                               mode="position"))
    assert rep.identical and rep.equal == 40
    assert "Farq yo'q: 40 ta yozuv bir xil" in win.summary.full_text()
    assert win._result_rows() == 0


def test_open_as_record_sets(qtbot, app, opened, files, windows) -> None:
    sa = opened(files[0])
    sb = opened(files[1])
    win = new_window(app, windows)
    run_files(qtbot, win, cw.CompareSpec(cw.FileSide(session=sa, path=sa.path), cw.FileSide(session=sb, path=sb.path),
                                         mode="key", key="id"))
    win._fill_records_menu()
    acts = {a.objectName(): a for a in win.records_menu.actions()}
    assert acts["compare_records_a"].isEnabled() and acts["compare_records_b"].isEnabled()
    with qtbot.waitSignal(win.records_opened, timeout=TIMEOUT):
        win.open_records("a")
    assert [sa.record_at(p) for p in range(sa.view_len())] == [5, 10]
    assert sa.view_label().startswith("Taqqoslash: 2 ta farqli yozuv (b.json bilan)")
    with qtbot.waitSignal(win.records_opened, timeout=TIMEOUT):
        win.open_records("b")
    assert [sb.record_at(p) for p in range(sb.view_len())] == [5, 39]


@pytest.mark.parametrize("fmt", ["json", "jsonl", "csv"])
def test_export_report(qtbot, app, files, tmp_path, windows, fmt) -> None:
    win = new_window(app, windows)
    run_files(qtbot, win, cw.CompareSpec(cw.FileSide(path=str(files[0])), cw.FileSide(path=str(files[1])),
                                         mode="key", key="id"))
    out = tmp_path / f"report.{fmt}"
    win.ask_save_path = lambda suggested: str(out)
    with qtbot.waitSignal(win.export_finished, timeout=TIMEOUT) as blocker:
        assert win.choose_export_file() is not None
    assert blocker.args[0] == 3
    assert "Hisobot saqlandi: 3 ta yozuv" in win.summary.full_text()
    if fmt == "json":
        data = json.loads(out.read_text(encoding="utf-8"))
        assert [r["status"] for r in data["records"]] == ["changed", "removed", "added"]
        assert data["records"][0]["changes"][0]["path"] == "name" and data["summary"]["changed"] == 1
    elif fmt == "jsonl":
        lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
        assert [x.get("status") for x in lines[:3]] == ["changed", "removed", "added"] and "summary" in lines[3]
    else:
        with out.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f, delimiter=app.settings.csv_delimiter))
        assert rows[0][:4] == ["status", "key", "a_index", "b_index"] and len(rows) == 4
    # the source files are refused
    assert win.export_report(str(files[0])) is None
    assert "manba" in win.summary.full_text().lower() or "source" in win.summary.full_text().lower() \
        or str(files[0]) in win.summary.full_text()


def test_cancel_a_run(qtbot, app, files, windows, monkeypatch) -> None:
    started = threading.Event()

    def slow(doc_a, doc_b, **kw):
        started.set()
        cancel = kw["cancel"]
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            cancel.check()
            time.sleep(0.01)
        raise AssertionError("not cancelled")

    monkeypatch.setattr(cw, "diff_files", slow)
    win = new_window(app, windows)
    with qtbot.waitSignal(win.run_finished, timeout=TIMEOUT) as blocker:
        win.compare_files(cw.CompareSpec(cw.FileSide(path=str(files[0])), cw.FileSide(path=str(files[1])),
                                         mode="key", key="id"))
        assert win.run_state() == "running" and win.cancel_button.isVisible()
        qtbot.waitUntil(started.is_set, timeout=TIMEOUT)
        win.cancel_button.click()
    assert blocker.args[0] is None
    assert win.run_state() == "cancelled" and win.summary.full_text() == "Taqqoslash bekor qilindi."
    assert not win.cancel_button.isVisible()


def test_edited_tab_is_compared_as_shown_and_goes_stale(qtbot, app, opened, files, windows) -> None:
    sa = opened(files[0])
    qtbot.waitUntil(lambda: sa.edit is not None and sa.edit.available, timeout=TIMEOUT)
    done: list = []
    sa.edit.set_value(NodeRef(3, ("name",)), '"tahrir"', on_done=done.append)
    qtbot.waitUntil(lambda: bool(done) and sa.dirty, timeout=TIMEOUT)
    win = new_window(app, windows)
    # the edited tab against its own file on disk: exactly the unsaved change
    rep = run_files(qtbot, win, cw.CompareSpec(cw.FileSide(session=sa, path=sa.path), cw.FileSide(path=sa.path),
                                               mode="position"))
    assert (rep.changed, rep.added, rep.removed) == (1, 0, 0)
    wait_view(qtbot, win)
    name = view_rows(win)["name"]
    assert (name.a_text, name.b_text) == ('"tahrir"', '"n3"')
    assert not win.is_stale() and not win.stale_bar.isVisible()
    done.clear()
    sa.edit.set_value(NodeRef(7, ("city",)), '"Buxoro"', on_done=done.append)
    qtbot.waitUntil(lambda: bool(done) and win.is_stale(), timeout=TIMEOUT)
    assert win.stale_bar.isVisible() and "a.json o'zgardi" in win.stale_bar.message()
    with qtbot.waitSignal(win.run_finished, timeout=TIMEOUT) as blocker:
        win.refresh()
    assert blocker.args[0].changed == 2 and not win.stale_bar.isVisible()


def test_closing_a_tab_marks_the_results_stale(qtbot, app, opened, files, windows) -> None:
    sa = opened(files[0])
    sb = opened(files[1])
    win = new_window(app, windows)
    run_files(qtbot, win, cw.CompareSpec(cw.FileSide(session=sa, path=sa.path), cw.FileSide(session=sb, path=sb.path),
                                         mode="key", key="id"))
    wait_rows(qtbot, win)
    sb.close()
    qtbot.waitUntil(win.is_stale, timeout=TIMEOUT)
    assert "b.json yopildi" in win.stale_bar.message()
    win._fill_records_menu()
    acts = {a.objectName(): a for a in win.records_menu.actions()}
    assert not acts["compare_records_b"].isEnabled() and acts["compare_records_a"].isEnabled()
    select_row(qtbot, win, 1)                       # still works (or fails gracefully): no crash
    QApplication.processEvents()
    with qtbot.waitSignal(win.run_finished, timeout=TIMEOUT) as blocker:
        win.refresh()                               # B is compared from its file now
    assert blocker.args[0].changed == 1 and win._spec.b.session is None


def test_single_documents_are_compared_as_nodes(qtbot, app, tmp_path, windows, backend) -> None:
    a = tmp_path / "s1.json"
    b = tmp_path / "s2.json"
    a.write_text('{"meta": {"v": 1.0, "name": "Ali"}, "list": [1, 2, 3]}', encoding="utf-8")
    b.write_text('{"meta": {"v": 1, "name": "Vali"}, "list": [0, 1, 2, 3]}', encoding="utf-8")
    win = new_window(app, windows)
    assert run_files(qtbot, win, cw.CompareSpec(cw.FileSide(path=str(a)), cw.FileSide(path=str(b)))) is None
    assert win.run_state() == "done"
    wait_view(qtbot, win)
    rows = [(r.key_label, r.status) for r in win.view.model().iter_rows()]
    assert ("name", "changed") in rows and ("v", "equal") in rows and ("[0]", "added") in rows
    assert not win.results.isVisible() and win.header.isVisible()


def test_wrapper_row(qtbot, app, tmp_path, windows) -> None:
    recs = ",".join(record_text(i) for i in range(5))
    a = tmp_path / "w1.json"
    b = tmp_path / "w2.json"
    a.write_text('{"meta": {"version": 1}, "data": [' + recs + ']}', encoding="utf-8")
    b.write_text('{"meta": {"version": 2}, "data": [' + recs + ']}', encoding="utf-8")
    win = new_window(app, windows)
    rep = run_files(qtbot, win, cw.CompareSpec(cw.FileSide(path=str(a)), cw.FileSide(path=str(b)), mode="position"))
    assert rep.wrapper is not None and rep.results_count == 0
    assert win._result_rows() == 1 and win.entry_of(0) == "wrapper"
    cell = win.results.source().cell(0, win.results.columns()[0])
    assert cell.text == "o'rovchi"
    wait_view(qtbot, win)
    version = view_rows(win)["version"]
    assert (version.a_text, version.b_text, version.status) == ("1", "2", "changed")


def test_too_big_without_a_key_shows_the_hint(qtbot, app, files, windows) -> None:
    app.settings = Settings(diff_align_max_bytes=100)
    win = new_window(app, windows)
    assert run_files(qtbot, win, cw.CompareSpec(cw.FileSide(path=str(files[0])), cw.FileSide(path=str(files[1])),
                                                mode="align")) is None
    assert win.run_state() == "failed"
    assert "kalitsiz to'liq taqqoslash uchun juda katta" in win.summary.full_text()
    assert win.warnings_label.isVisible() and "--key" in win.warnings_label.text()


def test_swap_and_options(qtbot, app, files, windows) -> None:
    win = new_window(app, windows)
    run_files(qtbot, win, cw.CompareSpec(cw.FileSide(path=str(files[0])), cw.FileSide(path=str(files[1])),
                                         mode="key", key="id"))
    with qtbot.waitSignal(win.run_finished, timeout=TIMEOUT) as blocker:
        win.swap_button.click()
    rep = blocker.args[0]
    assert (rep.added, rep.removed) == (1, 1) and win._spec.a.name == "b.json"
    rows = wait_rows(qtbot, win)
    assert [r.status for r in rows] == ["changed", "removed", "added"]
    assert [(r.a, r.b) for r in rows] == [(5, 5), (39, None), (None, 10)]
    dlg = win.show_options()
    qtbot.addWidget(dlg)
    assert dlg.mode_combo.currentData() == "key" and dlg.key_combo.currentText() == "id"
    assert dlg.side_widgets["a"][1].text().endswith("b.json")
    dlg.mode_combo.setCurrentIndex(dlg.mode_combo.findData("position"))
    with qtbot.waitSignal(win.run_finished, timeout=TIMEOUT) as blocker:
        dlg.accept()
    assert blocker.args[0].mode == "position"


def test_numbers_option_reaches_the_run(qtbot, app, files, windows) -> None:
    win = new_window(app, windows)
    rep = run_files(qtbot, win, cw.CompareSpec(cw.FileSide(path=str(files[0])), cw.FileSide(path=str(files[1])),
                                               mode="key", key="id", opts=DiffOptions(numbers="text")))
    assert rep.changed == 2                                   # 1.10 vs 1.1 counts as text
    rows = wait_rows(qtbot, win)
    assert [r.a for r in rows if r.status == "changed"] == [5, 20]


def test_keyed_documents(qtbot, app, opened, tmp_path, windows) -> None:
    def keyed(path: Path, recs: dict[str, str]) -> Path:
        path.write_text("{\n" + ",\n".join(f'"{k}": {v}' for k, v in recs.items()) + "\n}", encoding="utf-8")
        return path

    a = {f"k{i}": record_text(i) for i in range(10)}
    b = dict(a)
    b["k3"] = record_text(3, "yangi nom")
    del b["k5"]
    b["k99"] = record_text(99)
    sa = opened(keyed(tmp_path / "ka.json", a))
    revealed: list[tuple[Any, NodeRef]] = []
    win = windows(cw.CompareWindow(app, reveal=lambda s, ref: revealed.append((s, ref))))
    win.show()
    # auto: two keyed documents are matched by their record keys ($key)
    rep = run_files(qtbot, win, cw.CompareSpec(cw.FileSide(session=sa, path=sa.path),
                                               cw.FileSide(path=str(keyed(tmp_path / "kb.json", b)))))
    assert rep.mode == "key" and rep.key == "$key" and (rep.changed, rep.added, rep.removed) == (1, 1, 1)
    rows = wait_rows(qtbot, win)
    assert [(r.status, r.key, r.path) for r in rows] == [("changed", "k3", "name"), ("removed", "k5", ""),
                                                         ("added", "k99", "")]
    wait_view(qtbot, win)
    name = view_rows(win)["name"]
    assert name.a_path == ("name",) and win.view.model().root_label == "#3 ↔ #3 · k3"
    menu = win.view.build_menu(name.row)
    next(x for x in menu.actions() if x.objectName() == "diff_reveal_a").trigger()
    assert revealed == [(sa, NodeRef(3, ("name",)))]
    # by position: the records are compared as {key: value}; the same key -> its value is shown
    with qtbot.waitSignal(win.run_finished, timeout=TIMEOUT) as blocker:
        win.compare_files(cw.CompareSpec(cw.FileSide(session=sa, path=sa.path),
                                         cw.FileSide(path=str(tmp_path / "kb.json")), mode="position"))
    assert blocker.args[0].mode == "position"
    rows = wait_rows(qtbot, win)
    assert rows[0].a == 3 and rows[0].path == "name"
    wait_view(qtbot, win)
    assert view_rows(win)["name"].a_path == ("name",) and win.view.model().skip == 0
    # positions 5.. hold other keys: the pair is shown as two one-member objects
    select_row(qtbot, win, 1)
    labels = [(r.key_label, r.status) for r in win.view.model().iter_rows()]
    assert ("k5", "removed") in labels and ("k6", "added") in labels


def test_a_new_run_supersedes_a_running_one(qtbot, app, files, windows) -> None:
    win = new_window(app, windows)
    first = cw.CompareSpec(cw.FileSide(path=str(files[0])), cw.FileSide(path=str(files[1])), mode="key", key="id")
    second = cw.CompareSpec(cw.FileSide(path=str(files[0])), cw.FileSide(path=str(files[1])), mode="position")
    assert win.compare_files(first) is not None
    with qtbot.waitSignal(win.run_finished, timeout=TIMEOUT) as blocker:
        win.compare_files(second)
    assert blocker.args[0] is not None and blocker.args[0].mode == "position"
    assert app.tasks.wait_idle(30)
    QApplication.processEvents()
    assert win.report().mode == "position" and win.run_state() == "done"
    assert not win.cancel_button.isVisible() and not win.progress.isVisible()
    rows = wait_rows(qtbot, win)
    assert rows and all(r.error == "" for r in rows)


def test_big_nodes_show_only_the_differences_and_text(qtbot, app, opened, files, windows, monkeypatch,
                                                       backend) -> None:
    monkeypatch.setattr(cw, "FULL_VALUE_MAX", 256)          # every record array counts as "big"
    sa = opened(files[0])
    sb = opened(files[1])
    win = windows(cw.compare_nodes(None, (sa, NodeRef(None, ())), (sb, NodeRef(None, ())), app=app))
    wait_view(qtbot, win)
    m = win.view.model()
    assert not m.can_show_all and m.only_changes
    assert win.view.only_check.isChecked() and not win.view.only_check.isEnabled()
    assert "faqat farqli yo'llar" in win.view.message()
    points = []
    row = m.next_change(-1)
    while row is not None:
        points.append((m.row(row).status, m.row(row).path))
        row = m.next_change(row)
    assert ("changed", (5, "name")) in points
    assert all(s in ("changed", "added", "removed") for s, _p in points)
    # the text mode reads the documents (pretty text of the exact tokens)
    win.view.set_text_mode(True)
    qtbot.waitUntil(win.view.text_ready, timeout=TIMEOUT)
    ta, tb = win.view.text_editors()
    assert "1e400" in ta.toPlainText() and "12345678901234567890" in tb.toPlainText()
    assert "changed" in win.view.line_statuses()
