"""BulkDialog with the real DocSession and EditController (docs/ARCHITECTURE.md §12.9).

The main flows of tests/test_gui_bulkdialog.py on the real stack: Preview through ``session.edit.bulk_preview``
(heavy task) -> exact counts -> Apply through the edit worker -> ``data_changed`` -> undo restores the records;
a preview made stale by another edit can never be applied (its files are gone at once); the current-view scope
(a real filter) and a selection scope (rows of the view) restrict the operation; Stop; closing the dialog
discards the preview; a dedupe preview applied as one structural step. No errors are reported and no task is
left behind.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from jsonreshape.bulk import BulkSpec
from jsonreshape.gui.bulkdialog import BulkDialog, SelectionScope
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef
from jsonreshape.i18n import t

pytestmark = pytest.mark.gui

TIMEOUT = 30000
CITIES = ["Toshkent", "Samarqand", "Buxoro", "O'zbekiston"]


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.error_reported.connect(ctx.errors.append)
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def session(app, qtbot, tmp_path: Path):
    made: list[DocSession] = []

    def factory(n: int = 40) -> DocSession:
        recs = [{"id": i % 25, "name": CITIES[i % 4], "n": i, "tags": ["a", " b  c "]} for i in range(n)]
        path = tmp_path / f"doc{len(made)}.json"
        path.write_text(json.dumps(recs, indent=2, ensure_ascii=False), encoding="utf-8")
        s = DocSession(app, str(path), use_cache=False)
        made.append(s)
        with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
            s.start()
        assert s.edit is not None
        qtbot.waitUntil(lambda: s.edit.available, timeout=TIMEOUT)
        return s

    yield factory
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)
    assert not app.errors, app.errors


@pytest.fixture
def dialog(qtbot):
    def make(s: DocSession, scope_default: str = "all", **kw) -> BulkDialog:
        dlg = BulkDialog(s, scope_default, **kw)
        qtbot.addWidget(dlg)
        return dlg

    return make


def preview(dlg: BulkDialog, qtbot):
    assert dlg.preview() is not None, dlg.status_label.text()
    qtbot.waitUntil(lambda: dlg.preview_result is not None and dlg.task is None, timeout=TIMEOUT)
    return dlg.preview_result


def names(s: DocSession) -> list[str]:
    doc = s.doc.pinned()
    return [doc.record(i)["name"] for i in range(doc.record_count)]


def test_preview_apply_and_undo(session, dialog, qtbot) -> None:
    s = session()
    dlg = dialog(s)
    dlg.set_spec(BulkSpec("replace", {"find": "toshkent", "replacement": "Tashkent"}))
    pv = preview(dlg, qtbot)
    assert (pv.scanned, pv.records_changed, pv.values_changed) == (40, 10, 10)
    assert pv.gen == s.edit.applied_gen and dlg.can_apply()
    assert dlg.examples_table.item(0, 2).text() == '"Toshkent"' and dlg.examples_table.item(0, 3).text() == '"Tashkent"'
    applied: list = []
    dlg.applied.connect(applied.append)
    changes: list = []
    s.data_changed.connect(changes.append)
    assert dlg.apply()
    qtbot.waitUntil(lambda: bool(applied), timeout=TIMEOUT)
    assert applied[0].kind == "bulk" and changes and changes[-1] is applied[0]
    assert names(s).count("Tashkent") == 10 and "Toshkent" not in names(s)
    assert s.edit.dirty and dlg.status_label.text() == t("gui.bulk.status.applied", label=pv.spec.label())
    got: list = []
    assert s.edit.undo(on_done=got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    assert names(s).count("Toshkent") == 10


def test_stale_preview_after_another_edit(session, dialog, qtbot) -> None:
    s = session()
    dlg = dialog(s)
    dlg.set_spec(BulkSpec("clean_strings", {"trim": True, "collapse_spaces": True}))
    pv = preview(dlg, qtbot)
    assert pv.values_changed == 40 and dlg.can_apply()
    layer = Path(s.edit._worker.session.store.dir) / pv.layer
    assert layer.exists()
    got: list = []
    assert s.edit.set_value(NodeRef(0, ("name",)), '"boshqa"', on_done=got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    assert not dlg.can_apply() and dlg.status_label.text() == t("gui.bulk.status.stale")
    assert pv.discarded and not layer.exists()
    assert dlg.apply() is False
    pv2 = preview(dlg, qtbot)
    assert pv2.gen == s.edit.applied_gen and dlg.can_apply()


def test_view_and_selection_scopes(session, dialog, qtbot) -> None:
    s = session()
    s.set_filter(FilterSpec('name == "Buxoro"'))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 10, timeout=TIMEOUT)
    dlg = dialog(s, "view")
    assert dlg.scope_buttons["view"].text() == t("gui.bulk.scope.view", count="10")
    dlg.set_spec(BulkSpec("set_field", {"path": "checked", "value": "true"}))
    pv = preview(dlg, qtbot)
    assert pv.scanned == 10 and pv.records_changed == 10
    dlg.set_selection_source(lambda: SelectionScope.for_view_rows(s, [(0, 2, 0, 0)]))
    dlg.set_scope_choice("selected")
    assert dlg.scope_buttons["selected"].text() == t("gui.bulk.scope.selected", count="3")
    pv = preview(dlg, qtbot)
    assert pv.scanned == 3 and pv.records_changed == 3
    applied: list = []
    dlg.applied.connect(applied.append)
    assert dlg.apply()
    qtbot.waitUntil(lambda: bool(applied), timeout=TIMEOUT)
    doc = s.doc.pinned()
    checked = [i for i in range(doc.record_count) if doc.record(i).get("checked") is True]
    assert checked == [2, 6, 10]                                   # the first three Buxoro records


def test_stop_and_close(session, dialog, qtbot, monkeypatch) -> None:
    from jsonreshape import bulk

    gate = threading.Event()
    real = bulk.preview

    def gated(*args, cancel=None, **kw):                             # the preview waits until it is released
        while not gate.wait(0.01):
            cancel.check()
        return real(*args, cancel=cancel, **kw)

    monkeypatch.setattr(bulk, "preview", gated)
    s = session()
    dlg = dialog(s)
    failed: list = []
    dlg.preview_failed.connect(failed.append)
    dlg.set_spec(BulkSpec("delete_field", {"path": "tags"}))
    task = dlg.preview()
    assert task is not None and not dlg.stop_button.isHidden()
    dlg.stop()
    qtbot.waitUntil(lambda: dlg.task is None, timeout=TIMEOUT)
    assert dlg.status_label.text() == t("gui.bulk.status.cancelled") and len(failed) == 1
    assert dlg.preview_result is None and dlg.preview_button.isEnabled()
    gate.set()
    pv = preview(dlg, qtbot)
    layer = Path(s.edit._worker.session.store.dir) / pv.layer
    assert layer.exists()
    dlg.reject()
    assert pv.discarded and not layer.exists()


def test_selected_rows_of_a_real_table(session, qtbot) -> None:
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    from jsonreshape.gui.tableview import TableView

    s = session()
    s.set_filter(FilterSpec('name == "Samarqand"'))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 10, timeout=TIMEOUT)

    class Window(QWidget):
        def active_view(self):
            return view

    window = Window()
    qtbot.addWidget(window)
    view = TableView(s)
    QVBoxLayout(window).addWidget(view)
    window.resize(800, 500)
    window.show()
    view.activate()
    qtbot.waitUntil(lambda: view.grid.row_count() == 10, timeout=TIMEOUT)
    view.grid.set_selected_ranges([(2, 4, 0, 0)])                     # rows 2-4 of the filtered view
    dlg = BulkDialog(s, "selected", parent=window)                    # as the main window creates it
    qtbot.addWidget(dlg)
    assert dlg.scope_choice() == "selected"
    assert dlg.scope_buttons["selected"].text() == t("gui.bulk.scope.selected", count="3")
    dlg.set_spec(BulkSpec("set_field", {"path": "picked", "value": "true"}))
    pv = preview(dlg, qtbot)
    assert pv.scanned == 3
    applied: list = []
    dlg.applied.connect(applied.append)
    assert dlg.apply()
    qtbot.waitUntil(lambda: bool(applied), timeout=TIMEOUT)
    doc = s.doc.pinned()
    assert [i for i in range(doc.record_count) if doc.record(i).get("picked")] == [9, 13, 17]
    dlg.reject()
    view.shutdown()


def test_dedupe_through_the_controller(session, dialog, qtbot) -> None:
    s = session()
    dlg = dialog(s)
    dlg.set_spec(BulkSpec("dedupe", {"key": "id", "keep": "first"}))
    pv = preview(dlg, qtbot)
    assert pv.records_changed == 15
    applied: list = []
    dlg.applied.connect(applied.append)
    assert dlg.apply()
    qtbot.waitUntil(lambda: bool(applied), timeout=TIMEOUT)
    assert s.doc.record_count == 25 and applied[0].remap is not None
    assert dlg.scope_buttons["all"].text() == t("gui.bulk.scope.all", count="25")
